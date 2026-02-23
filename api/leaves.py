"""
Leave management router
"""

import json
import logging
import os
import calendar as cal_module
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.orm import joinedload

from sqlalchemy import func

from models.database import (
    get_session, Employee, LeaveRecord, LeaveQuota,
    ShiftAssignment, ShiftType, DailyShift, Holiday,
)
from utils.auth import require_permission
from utils.permissions import Permission

_UPLOAD_DIR = "uploads/leave_attachments"


def _parse_paths(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["leaves"])


# ============ Constants ============

# 請假扣薪規則（依勞基法）
LEAVE_DEDUCTION_RULES = {
    "personal": 1.0,   # 事假: 全扣
    "sick": 0.5,        # 病假: 扣半薪
    "menstrual": 0.5,   # 生理假: 扣半薪
    "annual": 0.0,      # 特休: 不扣
    "maternity": 0.0,   # 產假: 不扣
    "paternity": 0.0,   # 陪產假: 不扣（舊）
    "official": 0.0,    # 公假: 不扣
    "marriage": 0.0,    # 婚假: 不扣
    "bereavement": 0.0, # 喪假: 不扣
    "prenatal": 0.0,    # 產檢假: 不扣
    "paternity_new": 0.0,  # 陪產檢及陪產假: 不扣
    "miscarriage": 0.0, # 流產假: 不扣
    "family_care": 1.0, # 家庭照顧假: 不給薪（併入事假）
    "parental_unpaid": 0.0,  # 育嬰留職停薪: 不扣（留停期間無薪）
}

LEAVE_TYPE_LABELS = {
    "personal": "事假",
    "sick": "病假",
    "menstrual": "生理假",
    "annual": "特休",
    "maternity": "產假",
    "paternity": "陪產假",
    "official": "公假",
    "marriage": "婚假",
    "bereavement": "喪假",
    "prenatal": "產檢假",
    "paternity_new": "陪產檢及陪產假",
    "miscarriage": "流產假",
    "family_care": "家庭照顧假",
    "parental_unpaid": "育嬰留職停薪",
}


# ============ Pydantic Models ============

class LeaveCreate(BaseModel):
    employee_id: int
    leave_type: str
    start_date: date
    end_date: date
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    leave_hours: float = 8
    reason: Optional[str] = None

    @field_validator("leave_type")
    @classmethod
    def validate_leave_type(cls, v):
        if v not in LEAVE_DEDUCTION_RULES:
            raise ValueError(f"無效的假別: {v}")
        return v

    @field_validator("leave_hours")
    @classmethod
    def validate_leave_hours(cls, v):
        if v < 0.5:
            raise ValueError("請假時數至少 0.5 小時")
        if v > 480:
            raise ValueError("請假時數不得超過 480 小時")
        return v

    @model_validator(mode="after")
    def validate_date_order(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("結束日期不得早於開始日期")
        return self


class LeaveUpdate(BaseModel):
    leave_type: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    leave_hours: Optional[float] = None
    reason: Optional[str] = None

    @field_validator("leave_type")
    @classmethod
    def validate_leave_type(cls, v):
        if v is not None and v not in LEAVE_DEDUCTION_RULES:
            raise ValueError(f"無效的假別: {v}")
        return v

    @field_validator("leave_hours")
    @classmethod
    def validate_leave_hours(cls, v):
        if v is not None and v < 0.5:
            raise ValueError("請假時數至少 0.5 小時")
        return v

    @model_validator(mode="after")
    def validate_date_order(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("結束日期不得早於開始日期")
        return self


# ============ Helpers ============

def _check_overlap(
    session, employee_id: int, start_date: date, end_date: date, exclude_id: int = None
) -> "LeaveRecord | None":
    """檢查員工在指定日期區間是否已有未駁回的請假記錄（含待審與已核准）"""
    q = session.query(LeaveRecord).filter(
        LeaveRecord.employee_id == employee_id,
        LeaveRecord.start_date <= end_date,
        LeaveRecord.end_date >= start_date,
        LeaveRecord.is_approved.isnot(False),
    )
    if exclude_id is not None:
        q = q.filter(LeaveRecord.id != exclude_id)
    return q.first()


# ── 配額相關常數 ──────────────────────────────────────────────

# 有年度上限的假別（其餘為事件型，不追蹤）
QUOTA_LEAVE_TYPES: set[str] = {"annual", "sick", "menstrual", "personal", "family_care"}

# 法定年度配額（小時），不含特休（特休依年資動態計算）
STATUTORY_QUOTA_HOURS: dict[str, float] = {
    "sick":        240.0,   # 病假 30天
    "menstrual":    96.0,   # 生理假 12天
    "personal":    112.0,   # 事假 14天
    "family_care":  56.0,   # 家庭照顧假 7天（勞基法第20條）
}


def _calc_annual_leave_hours(hire_date: "date | None", year: int) -> float:
    """依勞基法第38條計算特休配額時數，以 year 年 12/31 為基準日計算年資"""
    if hire_date is None:
        return 0.0
    ref = date(year, 12, 31)
    if hire_date > ref:
        return 0.0

    # 完整月數（不足月不計）
    months = (ref.year - hire_date.year) * 12 + (ref.month - hire_date.month)
    if ref.day < hire_date.day:
        months -= 1

    if months < 6:
        return 0.0
    elif months < 12:
        return 24.0    # 3天
    complete_years = months // 12
    if complete_years < 2:
        return 56.0    # 7天
    elif complete_years < 3:
        return 80.0    # 10天
    elif complete_years < 5:
        return 112.0   # 14天
    elif complete_years < 10:
        return 120.0   # 15天
    else:
        days = min(15 + complete_years - 10, 30)
        return float(days * 8)


def _get_used_hours(session, employee_id: int, year: int, leave_type: str) -> float:
    """查詢該年度已核准的請假時數"""
    result = session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0)).filter(
        LeaveRecord.employee_id == employee_id,
        LeaveRecord.leave_type == leave_type,
        LeaveRecord.is_approved == True,
        func.strftime("%Y", LeaveRecord.start_date) == str(year),
    ).scalar()
    return float(result)


def _get_pending_hours(session, employee_id: int, year: int, leave_type: str) -> float:
    """查詢該年度待審核的請假時數"""
    result = session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0)).filter(
        LeaveRecord.employee_id == employee_id,
        LeaveRecord.leave_type == leave_type,
        LeaveRecord.is_approved.is_(None),
        func.strftime("%Y", LeaveRecord.start_date) == str(year),
    ).scalar()
    return float(result)


def _quota_row(session, quota: "LeaveQuota", year: int) -> dict:
    used = _get_used_hours(session, quota.employee_id, year, quota.leave_type)
    pending = _get_pending_hours(session, quota.employee_id, year, quota.leave_type)
    remaining = max(0.0, quota.total_hours - used)
    return {
        "id": quota.id,
        "employee_id": quota.employee_id,
        "year": quota.year,
        "leave_type": quota.leave_type,
        "leave_type_label": LEAVE_TYPE_LABELS.get(quota.leave_type, quota.leave_type),
        "total_hours": quota.total_hours,
        "used_hours": used,
        "pending_hours": pending,
        "remaining_hours": remaining,
        "note": quota.note,
    }


def _calc_shift_hours(work_start: str, work_end: str) -> float:
    """從 HH:MM 上下班時間計算有效工時，超過 5 小時自動扣除 1 小時午休"""
    sh, sm = map(int, work_start.split(":"))
    eh, em = map(int, work_end.split(":"))
    total_minutes = (eh * 60 + em) - (sh * 60 + sm)
    if total_minutes <= 0:
        total_minutes += 24 * 60  # 跨午夜班別（不常見但防呆）
    total_hours = total_minutes / 60
    if total_hours > 5:
        total_hours -= 1  # 扣除午休 1 小時
    return round(total_hours * 2) / 2  # 四捨五入至 0.5


# ============ Routes ============


@router.get("/leaves/workday-hours")
def get_workday_hours(
    employee_id: int,
    start_date: date,
    end_date: date,
    current_user: dict = Depends(require_permission(Permission.LEAVES_READ)),
):
    """
    計算員工在指定日期區間每日工作時數，整合：
    - 國定假日（Holiday 表）
    - 每日調班（DailyShift）
    - 每週排班（ShiftAssignment）
    - 週末自動排除
    - 無排班資料時預設 8h/天
    """
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="結束日期不得早於開始日期")
    if (end_date - start_date).days > 90:
        raise HTTPException(status_code=400, detail="查詢區間不得超過 90 天")

    session = get_session()
    try:
        # 1. 國定假日（區間內）
        holidays: dict[date, str] = {
            h.date: h.name
            for h in session.query(Holiday).filter(
                Holiday.date >= start_date,
                Holiday.date <= end_date,
                Holiday.is_active.is_(True),
            ).all()
        }

        # 2. 每日調班（DailyShift，含班別資料）
        daily_shifts: dict[date, ShiftType] = {
            ds.date: ds.shift_type
            for ds in session.query(DailyShift)
            .filter(
                DailyShift.employee_id == employee_id,
                DailyShift.date >= start_date,
                DailyShift.date <= end_date,
            )
            .options(joinedload(DailyShift.shift_type))
            .all()
        }

        # 3. 每週排班（含班別資料），取涵蓋整個區間的所有週一
        monday_start = start_date - timedelta(days=start_date.weekday())
        monday_end = end_date - timedelta(days=end_date.weekday())
        weekly_shifts: dict[date, ShiftType] = {
            a.week_start_date: a.shift_type
            for a in session.query(ShiftAssignment)
            .filter(
                ShiftAssignment.employee_id == employee_id,
                ShiftAssignment.week_start_date >= monday_start,
                ShiftAssignment.week_start_date <= monday_end,
            )
            .options(joinedload(ShiftAssignment.shift_type))
            .all()
        }

        breakdown = []
        total_hours = 0.0
        cur = start_date

        while cur <= end_date:
            weekday = cur.weekday()  # 0=Mon … 6=Sun

            if weekday >= 5:
                # 週末
                breakdown.append({
                    "date": cur.isoformat(),
                    "weekday": weekday,
                    "type": "weekend",
                    "hours": 0,
                    "shift": None,
                    "work_start": None,
                    "work_end": None,
                    "holiday_name": None,
                    "source": None,
                })
            elif cur in holidays:
                # 國定假日
                breakdown.append({
                    "date": cur.isoformat(),
                    "weekday": weekday,
                    "type": "holiday",
                    "hours": 0,
                    "shift": None,
                    "work_start": None,
                    "work_end": None,
                    "holiday_name": holidays[cur],
                    "source": None,
                })
            else:
                # 工作日 — 依優先順序取班別
                shift_type: ShiftType | None = None
                source = "default"

                if cur in daily_shifts:
                    shift_type = daily_shifts[cur]
                    source = "daily"
                else:
                    monday = cur - timedelta(days=weekday)
                    if monday in weekly_shifts:
                        shift_type = weekly_shifts[monday]
                        source = "weekly"

                if shift_type:
                    hours = _calc_shift_hours(shift_type.work_start, shift_type.work_end)
                    shift_name = shift_type.name
                    work_start = shift_type.work_start
                    work_end = shift_type.work_end
                else:
                    hours = 8.0
                    shift_name = None
                    work_start = None
                    work_end = None

                total_hours += hours
                breakdown.append({
                    "date": cur.isoformat(),
                    "weekday": weekday,
                    "type": "workday",
                    "hours": hours,
                    "shift": shift_name,
                    "work_start": work_start,
                    "work_end": work_end,
                    "holiday_name": None,
                    "source": source,
                })

            cur += timedelta(days=1)

        return {"total_hours": round(total_hours * 2) / 2, "breakdown": breakdown}
    finally:
        session.close()

@router.get("/leaves")
def get_leaves(
    employee_id: Optional[int] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    status: Optional[str] = None,
    current_user: dict = Depends(require_permission(Permission.LEAVES_READ)),
):
    """查詢請假記錄"""
    session = get_session()
    try:
        q = session.query(LeaveRecord, Employee).join(
            Employee, LeaveRecord.employee_id == Employee.id
        )
        if employee_id:
            q = q.filter(LeaveRecord.employee_id == employee_id)
        if status == "pending":
            q = q.filter(LeaveRecord.is_approved.is_(None))
        elif status == "approved":
            q = q.filter(LeaveRecord.is_approved == True)
        elif status == "rejected":
            q = q.filter(LeaveRecord.is_approved == False)
        if year and month:
            _, last_day = cal_module.monthrange(year, month)
            start = date(year, month, 1)
            end = date(year, month, last_day)
            q = q.filter(LeaveRecord.start_date <= end, LeaveRecord.end_date >= start)
        elif year:
            q = q.filter(LeaveRecord.start_date >= date(year, 1, 1), LeaveRecord.start_date <= date(year, 12, 31))

        records = q.order_by(LeaveRecord.start_date.desc()).all()

        results = []
        for leave, emp in records:
            results.append({
                "id": leave.id,
                "employee_id": leave.employee_id,
                "employee_name": emp.name,
                "leave_type": leave.leave_type,
                "leave_type_label": LEAVE_TYPE_LABELS.get(leave.leave_type, leave.leave_type),
                "start_date": leave.start_date.isoformat(),
                "end_date": leave.end_date.isoformat(),
                "start_time": leave.start_time,
                "end_time": leave.end_time,
                "leave_hours": leave.leave_hours,
                "deduction_ratio": LEAVE_DEDUCTION_RULES.get(leave.leave_type, 1.0),
                "reason": leave.reason,
                "is_approved": leave.is_approved,
                "approved_by": leave.approved_by,
                "rejection_reason": leave.rejection_reason,
                "attachment_paths": _parse_paths(leave.attachment_paths),
                "created_at": leave.created_at.isoformat() if leave.created_at else None,
            })
        return results
    finally:
        session.close()


# ── 配額 API ──────────────────────────────────────────────────

class QuotaUpdate(BaseModel):
    total_hours: float
    note: Optional[str] = None


@router.get("/leaves/quotas")
def get_leave_quotas(
    employee_id: Optional[int] = None,
    year: Optional[int] = None,
    leave_type: Optional[str] = None,
    current_user: dict = Depends(require_permission(Permission.LEAVES_READ)),
):
    """查詢請假配額，含動態計算已使用、待審、剩餘時數"""
    if year is None:
        year = date.today().year
    session = get_session()
    try:
        q = session.query(LeaveQuota).filter(LeaveQuota.year == year)
        if employee_id:
            q = q.filter(LeaveQuota.employee_id == employee_id)
        if leave_type:
            q = q.filter(LeaveQuota.leave_type == leave_type)
        quotas = q.order_by(LeaveQuota.employee_id, LeaveQuota.leave_type).all()
        return [_quota_row(session, quota, year) for quota in quotas]
    finally:
        session.close()


@router.post("/leaves/quotas/init", status_code=200)
def init_leave_quotas(
    employee_id: int,
    year: Optional[int] = None,
    current_user: dict = Depends(require_permission(Permission.LEAVES_WRITE)),
):
    """
    依勞基法自動初始化（或重新計算）指定員工的年度配額。
    - 特休：依 hire_date 年資計算
    - 其他有上限假別：使用法定固定時數
    - 已存在的配額只更新 total_hours 與 note，不刪除手動調整記錄
    """
    if year is None:
        year = date.today().year
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        upserted = []
        for lt in QUOTA_LEAVE_TYPES:
            if lt == "annual":
                hours = _calc_annual_leave_hours(emp.hire_date, year)
                if emp.hire_date:
                    ref = date(year, 12, 31)
                    months = (ref.year - emp.hire_date.year) * 12 + (ref.month - emp.hire_date.month)
                    if ref.day < emp.hire_date.day:
                        months -= 1
                    note = f"年資 {months // 12} 年 {months % 12} 個月（依勞基法第38條）"
                else:
                    note = "到職日未設定，配額為 0"
            else:
                hours = STATUTORY_QUOTA_HOURS[lt]
                note = "法定年度上限"

            quota = session.query(LeaveQuota).filter(
                LeaveQuota.employee_id == employee_id,
                LeaveQuota.year == year,
                LeaveQuota.leave_type == lt,
            ).first()
            if quota:
                quota.total_hours = hours
                quota.note = note
            else:
                quota = LeaveQuota(
                    employee_id=employee_id,
                    year=year,
                    leave_type=lt,
                    total_hours=hours,
                    note=note,
                )
                session.add(quota)
            upserted.append(lt)

        session.commit()
        logger.info(f"初始化員工 {emp.name}（ID:{employee_id}）{year} 年度假別配額：{upserted}")

        # 重新查詢後回傳
        quotas = session.query(LeaveQuota).filter(
            LeaveQuota.employee_id == employee_id,
            LeaveQuota.year == year,
        ).all()
        return [_quota_row(session, q, year) for q in quotas]
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/leaves/quotas/{quota_id}")
def update_leave_quota(
    quota_id: int,
    data: QuotaUpdate,
    current_user: dict = Depends(require_permission(Permission.LEAVES_WRITE)),
):
    """手動調整配額（例如主管核准額外特休）"""
    if data.total_hours < 0:
        raise HTTPException(status_code=400, detail="配額時數不得為負數")
    session = get_session()
    try:
        quota = session.query(LeaveQuota).filter(LeaveQuota.id == quota_id).first()
        if not quota:
            raise HTTPException(status_code=404, detail="配額記錄不存在")
        quota.total_hours = data.total_hours
        if data.note is not None:
            quota.note = data.note
        session.commit()
        return _quota_row(session, quota, quota.year)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ── 請假記錄 CRUD ──────────────────────────────────────────────

@router.post("/leaves", status_code=201)
def create_leave(data: LeaveCreate, current_user: dict = Depends(require_permission(Permission.LEAVES_WRITE))):
    """新增請假記錄"""
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == data.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        overlap = _check_overlap(session, data.employee_id, data.start_date, data.end_date)
        if overlap:
            raise HTTPException(
                status_code=409,
                detail=f"該員工在 {overlap.start_date} ~ {overlap.end_date} 已有請假記錄（ID: {overlap.id}），請確認後再新增"
            )

        leave = LeaveRecord(
            employee_id=data.employee_id,
            leave_type=data.leave_type,
            start_date=data.start_date,
            end_date=data.end_date,
            start_time=data.start_time,
            end_time=data.end_time,
            leave_hours=data.leave_hours,
            is_deductible=LEAVE_DEDUCTION_RULES[data.leave_type] > 0,
            deduction_ratio=LEAVE_DEDUCTION_RULES[data.leave_type],
            reason=data.reason,
        )
        session.add(leave)
        session.commit()
        return {"message": "請假記錄已新增", "id": leave.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/leaves/{leave_id}")
def update_leave(leave_id: int, data: LeaveUpdate, current_user: dict = Depends(require_permission(Permission.LEAVES_WRITE))):
    """更新請假記錄"""
    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail="請假記錄不存在")

        # 以更新後的日期範圍做重疊偵測（日期若未傳入則沿用原值）
        new_start = data.start_date or leave.start_date
        new_end = data.end_date or leave.end_date
        overlap = _check_overlap(session, leave.employee_id, new_start, new_end, exclude_id=leave_id)
        if overlap:
            raise HTTPException(
                status_code=409,
                detail=f"修改後的日期與現有請假記錄重疊（{overlap.start_date} ~ {overlap.end_date}，ID: {overlap.id}）"
            )

        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                setattr(leave, key, value)
        if data.leave_type and data.leave_type in LEAVE_DEDUCTION_RULES:
            leave.is_deductible = LEAVE_DEDUCTION_RULES[data.leave_type] > 0
            leave.deduction_ratio = LEAVE_DEDUCTION_RULES[data.leave_type]

        session.commit()
        return {"message": "請假記錄已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/leaves/{leave_id}")
def delete_leave(leave_id: int, current_user: dict = Depends(require_permission(Permission.LEAVES_WRITE))):
    """刪除請假記錄"""
    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail="請假記錄不存在")
        session.delete(leave)
        session.commit()
        return {"message": "請假記錄已刪除"}
    finally:
        session.close()


class ApproveRequest(BaseModel):
    approved: bool
    rejection_reason: Optional[str] = None


@router.put("/leaves/{leave_id}/approve")
def approve_leave(
    leave_id: int,
    data: ApproveRequest,
    current_user: dict = Depends(require_permission(Permission.LEAVES_WRITE)),
):
    """核准/駁回請假。駁回時 rejection_reason 為必填。"""
    if not data.approved and not (data.rejection_reason or "").strip():
        raise HTTPException(status_code=400, detail="駁回時必須填寫原因")
    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail="請假記錄不存在")
        leave.is_approved = data.approved
        leave.approved_by = current_user.get("username", "管理員") if data.approved else None
        leave.rejection_reason = data.rejection_reason.strip() if not data.approved and data.rejection_reason else None
        session.commit()
        return {"message": "已核准" if data.approved else "已駁回"}
    finally:
        session.close()


@router.get("/leaves/{leave_id}/attachments/{filename}")
def get_leave_attachment(
    leave_id: int,
    filename: str,
    current_user: dict = Depends(require_permission(Permission.LEAVES_READ)),
):
    """取得假單附件（管理後台）"""
    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail="找不到請假記錄")

        paths = _parse_paths(leave.attachment_paths)
        if filename not in paths:
            raise HTTPException(status_code=404, detail="找不到附件")

        file_path = os.path.join(_UPLOAD_DIR, str(leave_id), filename)
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="檔案不存在")

        return FileResponse(file_path)
    finally:
        session.close()
