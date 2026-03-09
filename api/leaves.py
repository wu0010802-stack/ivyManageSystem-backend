"""
Leave management router
"""

import json
import logging
import os
import calendar as cal_module
from pathlib import Path
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from models.database import (
    get_session, Employee, LeaveRecord, LeaveQuota,
    SalaryRecord,
)
from utils.auth import require_permission
from utils.permissions import Permission
from api.leaves_quota import (
    quota_router,
    LEAVE_TYPE_LABELS,
    LEAVE_DEDUCTION_RULES,
    _check_leave_limits,
    _check_quota,
)
from api.leaves_workday import workday_router

_UPLOAD_BASE = Path(__file__).resolve().parent.parent / "uploads" / "leave_attachments"


def _parse_paths(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _safe_attach_path(leave_id: int, filename: str) -> Path:
    """解析附件路徑並確認落在 _UPLOAD_BASE 之內（路徑穿越防護）。"""
    resolved = (_UPLOAD_BASE / str(leave_id) / filename).resolve()
    try:
        resolved.relative_to(_UPLOAD_BASE.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="無效的附件路徑")
    return resolved


logger = logging.getLogger(__name__)


def _check_salary_months_not_finalized(session, employee_id: int, months: set) -> None:
    """commit 前的封存保護守衛。

    若 months 中任何一個月份的薪資記錄已封存（is_finalized=True），
    拋出 409 阻止整個操作，避免 DB 進入「假單改了、薪資沒改」的矛盾狀態。

    Args:
        session:     SQLAlchemy session
        employee_id: 員工 ID
        months:      待檢查的 {(year, month), ...}，空集合直接返回
    """
    for yr, mo in months:
        record = session.query(SalaryRecord).filter(
            SalaryRecord.employee_id == employee_id,
            SalaryRecord.salary_year == yr,
            SalaryRecord.salary_month == mo,
            SalaryRecord.is_finalized == True,
        ).first()
        if record:
            by = record.finalized_by or "系統"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{yr} 年 {mo} 月薪資已封存（結算人：{by}），"
                    "無法修改該月份的假單。請先至薪資管理頁面解除封存後再操作。"
                ),
            )


router = APIRouter(prefix="/api", tags=["leaves"])
router.include_router(quota_router)
router.include_router(workday_router)

# ============ Service Injection ============

_salary_engine = None


def init_leaves_services(salary_engine_instance):
    global _salary_engine
    _salary_engine = salary_engine_instance


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
    deduction_ratio: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="扣薪比例覆蓋（不提供則依假別預設值，0.0=全薪，1.0=全扣）"
    )

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
        if round(v * 2) != v * 2:
            raise ValueError("請假時數必須為 0.5 小時的倍數（如 0.5、1、1.5、2…）")
        return v

    @model_validator(mode="after")
    def validate_date_order(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("結束日期不得早於開始日期")
        if self.start_date and self.end_date and (
            self.start_date.year != self.end_date.year
            or self.start_date.month != self.end_date.month
        ):
            raise ValueError(
                "請假區間不可跨月，若需跨越月底請拆成兩張假單分別申請"
                f"（本次 {self.start_date.year}/{self.start_date.month:02d} 月 →"
                f" {self.end_date.year}/{self.end_date.month:02d} 月）"
            )
        return self


class LeaveUpdate(BaseModel):
    leave_type: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    leave_hours: Optional[float] = None
    reason: Optional[str] = None
    deduction_ratio: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="扣薪比例覆蓋（不提供則依假別預設值，0.0=全薪，1.0=全扣）"
    )

    @field_validator("leave_type")
    @classmethod
    def validate_leave_type(cls, v):
        if v is not None and v not in LEAVE_DEDUCTION_RULES:
            raise ValueError(f"無效的假別: {v}")
        return v

    @field_validator("leave_hours")
    @classmethod
    def validate_leave_hours(cls, v):
        if v is not None:
            if v < 0.5:
                raise ValueError("請假時數至少 0.5 小時")
            if v > 480:
                raise ValueError("請假時數不得超過 480 小時")
            if round(v * 2) != v * 2:
                raise ValueError("請假時數必須為 0.5 小時的倍數（如 0.5、1、1.5、2…）")
        return v

    @model_validator(mode="after")
    def validate_date_order(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("結束日期不得早於開始日期")
        if self.start_date and self.end_date and (
            self.start_date.year != self.end_date.year
            or self.start_date.month != self.end_date.month
        ):
            raise ValueError(
                "請假區間不可跨月，若需跨越月底請拆成兩張假單分別申請"
            )
        return self


# ============ Helpers ============

def _check_overlap(
    session,
    employee_id: int,
    start_date: date,
    end_date: date,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    exclude_id: int = None,
) -> "LeaveRecord | None":
    """檢查員工在指定日期區間（含時段）是否已有「已核准」的請假記錄。
    待審核記錄不列入封鎖，允許員工同時提交多份申請供主管選擇。

    時段重疊規則：
    - 若任一方跨多天 → 純日期重疊即視為衝突
    - 若雙方都是同一天的單日假單，且雙方都提供了 start_time/end_time
      → 做時間區間精確比對，不重疊則放行
      （不重疊條件：new_end <= exist_start 或 exist_end <= new_start）
    - 其餘情況（缺乏時間資訊）→ 同日即視為衝突
    """
    q = session.query(LeaveRecord).filter(
        LeaveRecord.employee_id == employee_id,
        LeaveRecord.start_date <= end_date,
        LeaveRecord.end_date >= start_date,
        LeaveRecord.is_approved == True,
    )
    if exclude_id is not None:
        q = q.filter(LeaveRecord.id != exclude_id)

    is_new_single_day = (start_date == end_date)

    for record in q.all():
        is_record_single_day = (record.start_date == record.end_date)

        # 雙方都是同一天的單日假單，且雙方都有時間資訊 → 做時間段精確比對
        if (
            is_new_single_day
            and is_record_single_day
            and start_time
            and end_time
            and record.start_time
            and record.end_time
        ):
            # HH:MM 字串可直接做字典序比較（00:00 ~ 23:59 均正確）
            if end_time <= record.start_time or record.end_time <= start_time:
                continue  # 時間不重疊，放行

        return record  # 日期重疊且不符合放行條件 → 衝突

    return None


# ============ Routes ============


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


# ── 請假記錄 CRUD ──────────────────────────────────────────────

@router.post("/leaves", status_code=201)
def create_leave(data: LeaveCreate, current_user: dict = Depends(require_permission(Permission.LEAVES_WRITE))):
    """新增請假記錄"""
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == data.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        overlap = _check_overlap(
            session, data.employee_id, data.start_date, data.end_date,
            data.start_time, data.end_time,
        )
        if overlap:
            raise HTTPException(
                status_code=409,
                detail=f"該員工在 {overlap.start_date} ~ {overlap.end_date} 已有已核准的請假記錄（ID: {overlap.id}），無法重複請假"
            )

        _check_leave_limits(
            session, data.employee_id, data.leave_type,
            data.start_date, data.leave_hours
        )
        _check_quota(
            session, data.employee_id, data.leave_type,
            data.start_date.year, data.leave_hours
        )

        # 優先使用 API 傳入的覆蓋值；未提供則依假別預設規則
        effective_ratio = data.deduction_ratio \
            if data.deduction_ratio is not None \
            else LEAVE_DEDUCTION_RULES[data.leave_type]
        leave = LeaveRecord(
            employee_id=data.employee_id,
            leave_type=data.leave_type,
            start_date=data.start_date,
            end_date=data.end_date,
            start_time=data.start_time,
            end_time=data.end_time,
            leave_hours=data.leave_hours,
            is_deductible=effective_ratio > 0,
            deduction_ratio=effective_ratio,
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
    """更新請假記錄。若記錄已核准，修改後自動退回「待審核」狀態以符合稽核要求。"""
    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail="請假記錄不存在")

        # 記錄修改前的核准狀態（供後續稽核退審判斷）
        was_approved = leave.is_approved == True
        # 在 setattr 套用新日期前，捕捉原始月份（日期修改後此值會被覆蓋）
        orig_month = (leave.start_date.year, leave.start_date.month)

        # 以更新後的日期 / 時間做重疊偵測（未傳入的欄位沿用原值）
        new_start = data.start_date or leave.start_date
        new_end = data.end_date or leave.end_date

        # 跨月檢查：更新後的區間也不允許跨月
        if new_start.year != new_end.year or new_start.month != new_end.month:
            raise HTTPException(
                status_code=400,
                detail=(
                    "請假區間不可跨月，若需跨越月底請拆成兩張假單分別申請"
                    f"（更新後 {new_start.year}/{new_start.month:02d} 月 →"
                    f" {new_end.year}/{new_end.month:02d} 月）"
                ),
            )

        new_start_time = data.start_time if data.start_time is not None else leave.start_time
        new_end_time = data.end_time if data.end_time is not None else leave.end_time
        overlap = _check_overlap(
            session, leave.employee_id, new_start, new_end,
            new_start_time, new_end_time, exclude_id=leave_id,
        )
        if overlap:
            raise HTTPException(
                status_code=409,
                detail=f"修改後的日期與已核准的請假記錄重疊（{overlap.start_date} ~ {overlap.end_date}，ID: {overlap.id}）"
            )

        new_type = data.leave_type or leave.leave_type
        new_hours = data.leave_hours if data.leave_hours is not None else leave.leave_hours
        # 已核准的假單退審後視同重新提交：用 include_pending=True 重新過一次配額（排除自身）
        _check_leave_limits(
            session, leave.employee_id, new_type,
            new_start, new_hours, exclude_id=leave_id
        )
        _check_quota(
            session, leave.employee_id, new_type,
            new_start.year, new_hours, exclude_id=leave_id
        )

        # ── 封存月薪保護（must be BEFORE commit）────────────────────────────────
        # 修改已核准假單會觸發薪資重算；若該月薪資已封存，必須在 commit 前阻擋，
        # 否則假單改了、薪資沒改，DB 永遠處於矛盾狀態。
        # 同時檢查「原始月份」與「更新後月份」（日期可能被修改到不同月）。
        if was_approved:
            _check_salary_months_not_finalized(
                session, leave.employee_id,
                {orig_month, (new_start.year, new_start.month)},
            )

        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                setattr(leave, key, value)
        # 假別更換時重設 deduction_ratio，但若本次同時明確傳入 deduction_ratio 則以傳入值為準
        if data.leave_type and data.leave_type in LEAVE_DEDUCTION_RULES:
            if data.deduction_ratio is None:
                # 假別改變，未明確指定比例 → 使用新假別的預設規則
                leave.deduction_ratio = LEAVE_DEDUCTION_RULES[data.leave_type]
            # 若有明確 deduction_ratio 傳入，已在上方 setattr 迴圈中套用
            leave.is_deductible = leave.deduction_ratio > 0

        # ── 稽核退審：已核准的記錄被修改，自動退回待審核 ──────────────────────
        # 防止管理員靜默竄改已核准假單時數/日期，導致薪資扣款異常（財務防呆）
        if was_approved:
            leave.is_approved = None
            leave.approved_by = None
            leave.rejection_reason = None
            logger.warning(
                "稽核警告：已核准請假記錄 #%d（員工 ID=%d, %s~%s, %s）被管理員「%s」修改，"
                "已自動退回待審核狀態，需重新核准",
                leave_id, leave.employee_id, leave.start_date, leave.end_date,
                leave.leave_type, current_user.get("username", "unknown"),
            )

        session.commit()

        result = {"message": "請假記錄已更新"}
        if was_approved:
            result["message"] += "；原核准狀態已自動退回「待審核」，請重新送審"
            result["reset_to_pending"] = True
            # 薪資重算：撤銷原核准假單在薪資中的扣款
            if _salary_engine is not None:
                try:
                    emp_id = leave.employee_id
                    months_to_recalc: set = set()
                    cur = date(leave.start_date.year, leave.start_date.month, 1)
                    end_m = date(leave.end_date.year, leave.end_date.month, 1)
                    while cur <= end_m:
                        months_to_recalc.add((cur.year, cur.month))
                        cur = (
                            date(cur.year + 1, 1, 1) if cur.month == 12
                            else date(cur.year, cur.month + 1, 1)
                        )
                    for yr, mo in sorted(months_to_recalc):
                        _salary_engine.process_salary_calculation(emp_id, yr, mo)
                    result["salary_recalculated"] = True
                except Exception as e:
                    result["salary_warning"] = "薪資重算失敗，請手動前往薪資頁面重新計算"
                    logger.error("請假修改退審後薪資重算失敗：%s", e)

        return result
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

        # ── 封存保護：已核准假單在封存月份不得刪除 ──────────────────────────
        was_approved = leave.is_approved is True
        leave_month = (leave.start_date.year, leave.start_date.month)
        emp_id = leave.employee_id
        if was_approved:
            _check_salary_months_not_finalized(session, emp_id, {leave_month})

        session.delete(leave)
        session.commit()

        result = {"message": "請假記錄已刪除"}
        # 刪除已核准假單後補算薪資，撤銷原扣款
        if was_approved and _salary_engine is not None:
            try:
                _salary_engine.process_salary_calculation(emp_id, *leave_month)
                result["salary_recalculated"] = True
            except Exception as e:
                result["salary_warning"] = "假單已刪除，但薪資重算失敗，請手動前往薪資頁面重新計算"
                logger.error("刪除假單後薪資重算失敗：%s", e)

        return result
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
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

        warning = None
        if data.approved:
            # 提示主管：該員工同期是否已有其他已核准假單（含時段比對，不強制阻擋，由主管判斷）
            conflict = _check_overlap(
                session, leave.employee_id, leave.start_date, leave.end_date,
                leave.start_time, leave.end_time,
                exclude_id=leave_id,
            )
            if conflict:
                warning = (
                    f"注意：該員工在 {conflict.start_date} ~ {conflict.end_date} "
                    f"已有另一筆已核准的請假（ID: {conflict.id}），請確認是否重複核准"
                )

            # ── 配額硬檢查（核准動作）──────────────────────────────────────────
            # 此假單目前仍是待審（is_approved=None），不在 approved 計數內，
            # 故用 include_pending=False 直接檢查「已核准 + 本次」是否超出年度配額。
            # 防止主管把多張待審假單全部核准造成額度嚴重超支（-N 天）。
            _check_leave_limits(
                session, leave.employee_id, leave.leave_type,
                leave.start_date, leave.leave_hours,
                include_pending=False,
            )
            _check_quota(
                session, leave.employee_id, leave.leave_type,
                leave.start_date.year, leave.leave_hours,
                include_pending=False,
            )

            # ── 封存月薪保護（commit 前）────────────────────────────────────────
            # 核准假單會觸發薪資重算；若該月薪資已封存，必須在 commit 前阻擋，
            # 否則假單被核准、薪資沒更新，DB 永遠處於矛盾狀態。
            _check_salary_months_not_finalized(
                session, leave.employee_id,
                {(leave.start_date.year, leave.start_date.month)},
            )

        leave.is_approved = data.approved
        leave.approved_by = current_user.get("username", "管理員") if data.approved else None
        leave.rejection_reason = data.rejection_reason.strip() if not data.approved and data.rejection_reason else None
        session.commit()

        result = {"message": "已核准" if data.approved else "已駁回"}
        if warning:
            result["warning"] = warning

        # 核准後自動重算該員工所有涉及月份的薪資
        if data.approved and _salary_engine is not None:
            try:
                emp_id = leave.employee_id
                # 計算假單跨越的所有 (year, month)
                months_to_recalc = set()
                cur = date(leave.start_date.year, leave.start_date.month, 1)
                end = date(leave.end_date.year, leave.end_date.month, 1)
                while cur <= end:
                    months_to_recalc.add((cur.year, cur.month))
                    cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)

                for year, month in sorted(months_to_recalc):
                    _salary_engine.process_salary_calculation(emp_id, year, month)
                    logger.info(f"請假核准後自動重算薪資：emp_id={emp_id}, {year}/{month}")

                result["salary_recalculated"] = True
                result["message"] = "已核准，薪資已自動重算"
            except Exception as e:
                result["salary_recalculated"] = False
                result["salary_warning"] = "已核准，但薪資重算失敗，請手動前往薪資頁面重新計算"
                logger.error(f"請假核准後薪資重算失敗：{e}")

        return result
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

        file_path = _safe_attach_path(leave_id, filename)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="檔案不存在")

        return FileResponse(str(file_path))
    finally:
        session.close()
