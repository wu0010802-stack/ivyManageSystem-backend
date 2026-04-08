"""
Leave management router
"""

import json
import logging
import calendar as cal_module
from pathlib import Path
from datetime import date
from typing import Optional, List
from io import BytesIO

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from utils.errors import raise_safe_500
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from utils.leave_validators import validate_leave_hours_value, validate_leave_date_order
from sqlalchemy import or_, and_

from models.database import (
    get_session, Employee, LeaveRecord, LeaveQuota,
    OvertimeRecord, SalaryRecord, User,
)
from utils.auth import require_staff_permission
from utils.error_messages import EMPLOYEE_DOES_NOT_EXIST, LEAVE_RECORD_NOT_FOUND
from utils.permissions import Permission
from utils.approval_helpers import _get_submitter_role, _check_approval_eligibility, _write_approval_log
from utils.excel_utils import xlsx_streaming_response
from utils.import_utils import build_employee_lookup, resolve_employee_from_row
from utils.file_upload import read_upload_with_size_check, validate_file_signature
from api.leaves_quota import (
    quota_router,
    LEAVE_TYPE_LABELS,
    LEAVE_DEDUCTION_RULES,
    _check_leave_limits,
    _check_quota,
)
from api.leaves_workday import workday_router, validate_leave_hours_against_schedule
from services.leave_policy import requires_supporting_document

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
    if not months:
        return
    record = session.query(SalaryRecord).filter(
        SalaryRecord.employee_id == employee_id,
        SalaryRecord.is_finalized == True,
        or_(*(
            and_(SalaryRecord.salary_year == yr, SalaryRecord.salary_month == mo)
            for yr, mo in months
        )),
    ).first()
    if record:
        by = record.finalized_by or "系統"
        raise HTTPException(
            status_code=409,
            detail=(
                f"{record.salary_year} 年 {record.salary_month} 月薪資已封存（結算人：{by}），"
                "無法修改該月份的假單。請先至薪資管理頁面解除封存後再操作。"
            ),
        )


def _collect_leave_months(start_date, end_date) -> set:
    """收集假單所跨越的所有 (year, month)。"""
    months = set()
    current = start_date.replace(day=1)
    end_first = end_date.replace(day=1)
    while current <= end_first:
        months.add((current.year, current.month))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def _apply_leave_update_and_revoke(
    leave, data, current_user, leave_id: int
) -> None:
    """將 update 欄位套用到 leave，並在已核准時執行退審邏輯。

    此函式負責：
    1. 以 setattr 套用所有非 None 欄位
    2. 假別更換時重設 deduction_ratio / is_deductible
    3. 若原狀態為已核准，自動退回待審核並記錄警告

    Args:
        leave:        LeaveRecord ORM 物件（會被就地修改）
        data:         LeaveUpdate Pydantic schema
        current_user: 操作者資訊（供 audit log）
        leave_id:     假單 ID（供 audit log）
    """
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if value is not None:
            setattr(leave, key, value)

    # 假別更換時重設 deduction_ratio，但若本次同時明確傳入 deduction_ratio 則以傳入值為準
    if data.leave_type and data.leave_type in LEAVE_DEDUCTION_RULES:
        if data.deduction_ratio is None:
            leave.deduction_ratio = LEAVE_DEDUCTION_RULES[data.leave_type]
        leave.is_deductible = leave.deduction_ratio > 0

    # ── 稽核退審：已核准的記錄被修改，自動退回待審核 ──────────────────────
    was_approved = leave.is_approved == True
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


router = APIRouter(prefix="/api", tags=["leaves"])
router.include_router(quota_router)
router.include_router(workday_router)

# ============ Service Injection ============

_salary_engine = None
_line_service = None


def init_leaves_services(salary_engine_instance):
    global _salary_engine
    _salary_engine = salary_engine_instance


def init_leaves_line_service(line_service):
    global _line_service
    _line_service = line_service


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
        return validate_leave_hours_value(v)

    @model_validator(mode="after")
    def validate_date_order(self):
        validate_leave_date_order(self.start_date, self.end_date)
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


def _check_substitute_guard(leave, *, allow_without_substitute: bool = False) -> None:
    """序列式守衛：核准假單前確認代理人已接受。

    - pending  → 409（等待代理人回應）
    - rejected → 409（需重新指定代理人）
    - accepted / not_required → 放行
    """
    status = getattr(leave, "substitute_status", "not_required") or "not_required"
    if allow_without_substitute and status in {"pending", "rejected"}:
        return
    if status == "pending":
        raise HTTPException(
            status_code=409,
            detail="代理人尚未接受，請等待代理人回應後再核准",
        )
    if status == "rejected":
        raise HTTPException(
            status_code=409,
            detail="代理人已拒絕此代理請求，請要求員工重新指定代理人後再核准",
        )


def _find_overlapping_leave(
    session,
    employee_id: int,
    start_date: date,
    end_date: date,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    exclude_id: int = None,
    include_pending: bool = False,
) -> "LeaveRecord | None":
    """檢查員工在指定日期區間（含時段）是否已有重疊假單。

    預設只檢查已核准假單；`include_pending=True` 時，待審假單也視為衝突。

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
    )
    if include_pending:
        q = q.filter(or_(LeaveRecord.is_approved == True, LeaveRecord.is_approved.is_(None)))
    else:
        q = q.filter(LeaveRecord.is_approved == True)
    if exclude_id is not None:
        q = q.filter(LeaveRecord.id != exclude_id)

    is_new_single_day = (start_date == end_date)

    # 只有新假單是單日且提供時間資訊時，才能在 DB 層排除確定不重疊的單日記錄
    # HH:MM 字串字典序與時間順序一致，可直接在 SQL 比較
    if is_new_single_day and start_time and end_time:
        q = q.filter(
            ~and_(
                LeaveRecord.start_date == LeaveRecord.end_date,
                LeaveRecord.start_time.isnot(None),
                LeaveRecord.end_time.isnot(None),
                or_(
                    LeaveRecord.end_time <= start_time,
                    LeaveRecord.start_time >= end_time,
                ),
            )
        )

    return q.first()


def _check_overlap(
    session,
    employee_id: int,
    start_date: date,
    end_date: date,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    exclude_id: int = None,
) -> "LeaveRecord | None":
    """檢查員工在指定日期區間（含時段）是否已有「已核准」的請假記錄。"""
    return _find_overlapping_leave(
        session,
        employee_id,
        start_date,
        end_date,
        start_time=start_time,
        end_time=end_time,
        exclude_id=exclude_id,
        include_pending=False,
    )


def _check_substitute_leave_conflict(
    session,
    substitute_employee_id: Optional[int],
    start_date: date,
    end_date: date,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> None:
    """代理人不可在相同時段有待審或已核准的請假或加班記錄（V14）。"""
    if substitute_employee_id is None:
        return

    # ── 檢查請假衝突 ────────────────────────────────────────────────────
    leave_conflict = _find_overlapping_leave(
        session,
        substitute_employee_id,
        start_date,
        end_date,
        start_time=start_time,
        end_time=end_time,
        include_pending=True,
    )
    if leave_conflict:
        status_label = "待審核" if leave_conflict.is_approved is None else "已核准"
        raise HTTPException(
            status_code=409,
            detail=(
                f"代理人在 {leave_conflict.start_date} ~ {leave_conflict.end_date} "
                f"已有{status_label}請假記錄，請改派其他代理人"
            ),
        )

    # ── 檢查加班衝突（V14）──────────────────────────────────────────────
    # 代理人若有與請假時段重疊的待審/已核准加班記錄，同樣不適合擔任代理人
    ot_conflict = (
        session.query(OvertimeRecord)
        .filter(
            OvertimeRecord.employee_id == substitute_employee_id,
            OvertimeRecord.is_approved.in_([None, True]),
            OvertimeRecord.overtime_date >= start_date,
            OvertimeRecord.overtime_date <= end_date,
        )
        .first()
    )
    if ot_conflict:
        ot_status = "待審核" if ot_conflict.is_approved is None else "已核准"
        raise HTTPException(
            status_code=409,
            detail=(
                f"代理人在 {ot_conflict.overtime_date} 有{ot_status}加班記錄，"
                "代理人當日已安排加班，請改派其他代理人"
            ),
        )


# ============ Routes ============


@router.get("/leaves")
def get_leaves(
    employee_id: Optional[int] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    status: Optional[str] = None,
    current_user: dict = Depends(require_staff_permission(Permission.LEAVES_READ)),
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

        # 預先載入員工角色映射（減少逐筆查 DB 的 N+1 問題）
        from models.database import User as UserModel
        employee_ids = list({leave.employee_id for leave, _ in records})
        user_roles = {}
        if employee_ids:
            users = session.query(UserModel).filter(
                UserModel.employee_id.in_(employee_ids),
                UserModel.is_active == True,
            ).all()
            user_roles = {u.employee_id: u.role for u in users}

        # 預先載入代理人姓名（批次查詢，避免 N+1）
        substitute_ids = list({leave.substitute_employee_id for leave, _ in records if leave.substitute_employee_id})
        substitute_names: dict = {}
        if substitute_ids:
            subs = session.query(Employee).filter(Employee.id.in_(substitute_ids)).all()
            substitute_names = {s.id: s.name for s in subs}

        # 預先載入換班關聯（同員工、假單區間內有 pending/accepted 的換班申請）
        from models.database import ShiftSwapRequest
        # 只做一次聚合查詢，按 requester_id + swap_date 建立快速索引
        swap_map: dict = {}
        if records:
            involved_emp_ids = list({lv.employee_id for lv, _ in records})
            # 計算所有假單的整體日期範圍，在 DB 層過濾 swap_date，避免 Python 端大量掃描
            _all_starts = [lv.start_date for lv, _ in records]
            _all_ends = [lv.end_date for lv, _ in records]
            _swap_range_start = min(_all_starts)
            _swap_range_end = max(_all_ends)
            all_swaps = session.query(ShiftSwapRequest).filter(
                ShiftSwapRequest.requester_id.in_(involved_emp_ids),
                ShiftSwapRequest.status.in_(["pending", "accepted"]),
                ShiftSwapRequest.swap_date >= _swap_range_start,
                ShiftSwapRequest.swap_date <= _swap_range_end,
            ).all()
            for sw in all_swaps:
                swap_map.setdefault(sw.requester_id, []).append(sw)

        results = []
        for leave, emp in records:
            # 換班關聯：查詢同員工、同期間的換班申請
            related_swap = None
            for sw in swap_map.get(leave.employee_id, []):
                if leave.start_date <= sw.swap_date <= leave.end_date:
                    related_swap = {
                        "id": sw.id,
                        "swap_date": sw.swap_date.isoformat(),
                        "status": sw.status,
                        "target_id": sw.target_id,
                    }
                    break

            results.append({
                "id": leave.id,
                "employee_id": leave.employee_id,
                "employee_name": emp.name,
                "submitter_role": user_roles.get(leave.employee_id, "teacher"),
                "leave_type": leave.leave_type,
                "leave_type_label": LEAVE_TYPE_LABELS.get(leave.leave_type, leave.leave_type),
                "start_date": leave.start_date.isoformat(),
                "end_date": leave.end_date.isoformat(),
                "start_time": leave.start_time,
                "end_time": leave.end_time,
                "leave_hours": leave.leave_hours,
                "deduction_ratio": leave.deduction_ratio,
                "reason": leave.reason,
                "is_approved": leave.is_approved,
                "approved_by": leave.approved_by,
                "rejection_reason": leave.rejection_reason,
                "attachment_paths": _parse_paths(leave.attachment_paths),
                "substitute_employee_id": leave.substitute_employee_id,
                "substitute_employee_name": substitute_names.get(leave.substitute_employee_id),
                "substitute_status": leave.substitute_status or "not_required",
                "substitute_responded_at": leave.substitute_responded_at.isoformat() if leave.substitute_responded_at else None,
                "related_swap": related_swap,
                "created_at": leave.created_at.isoformat() if leave.created_at else None,
            })
        return results
    finally:
        session.close()


# ── 請假記錄 CRUD ──────────────────────────────────────────────

@router.post("/leaves", status_code=201)
def create_leave(data: LeaveCreate, current_user: dict = Depends(require_staff_permission(Permission.LEAVES_WRITE))):
    """新增請假記錄"""
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == data.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail=EMPLOYEE_DOES_NOT_EXIST)

        overlap = _check_overlap(
            session, data.employee_id, data.start_date, data.end_date,
            data.start_time, data.end_time,
        )
        if overlap:
            raise HTTPException(
                status_code=409,
                detail=f"該員工在 {overlap.start_date} ~ {overlap.end_date} 已有已核准的請假記錄（ID: {overlap.id}），無法重複請假"
            )

        validate_leave_hours_against_schedule(
            session,
            data.employee_id,
            data.start_date,
            data.end_date,
            data.leave_hours,
            data.start_time,
            data.end_time,
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
        if data.deduction_ratio is not None:
            default_ratio = LEAVE_DEDUCTION_RULES.get(data.leave_type, -1)
            logger.warning(
                "假單扣薪比例被手動覆蓋 by user=%s employee_id=%s leave_type=%s "
                "default_ratio=%s overridden_ratio=%s",
                current_user.get("username"), data.employee_id,
                data.leave_type, default_ratio, data.deduction_ratio,
            )
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
        raise_safe_500(e)
    finally:
        session.close()


@router.put("/leaves/{leave_id}")
def update_leave(leave_id: int, data: LeaveUpdate, current_user: dict = Depends(require_staff_permission(Permission.LEAVES_WRITE))):
    """更新請假記錄。若記錄已核准，修改後自動退回「待審核」狀態以符合稽核要求。"""
    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail=LEAVE_RECORD_NOT_FOUND)

        # 在 setattr 套用新日期前，捕捉原始狀態
        was_approved = leave.is_approved == True
        orig_month = (leave.start_date.year, leave.start_date.month)

        # 計算更新後的日期 / 時間（未傳入的欄位沿用原值）
        new_start = data.start_date or leave.start_date
        new_end = data.end_date or leave.end_date
        new_start_time = data.start_time if data.start_time is not None else leave.start_time
        new_end_time = data.end_time if data.end_time is not None else leave.end_time

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
        validate_leave_hours_against_schedule(
            session, leave.employee_id, new_start, new_end,
            new_hours, new_start_time, new_end_time,
        )
        # 已核准的假單退審後視同重新提交：重新過一次配額（排除自身）
        _check_leave_limits(session, leave.employee_id, new_type, new_start, new_hours, exclude_id=leave_id)
        _check_quota(session, leave.employee_id, new_type, new_start.year, new_hours, exclude_id=leave_id)

        # 封存月薪保護：同時檢查原始月份與更新後月份
        if was_approved:
            _check_salary_months_not_finalized(
                session, leave.employee_id,
                {orig_month, (new_start.year, new_start.month)},
            )

        _apply_leave_update_and_revoke(leave, data, current_user, leave_id)
        session.commit()

        result = {"message": "請假記錄已更新"}
        if was_approved:
            result["message"] += "；原核准狀態已自動退回「待審核」，請重新送審"
            result["reset_to_pending"] = True
            if _salary_engine is not None:
                try:
                    months_to_recalc = _collect_leave_months(leave.start_date, leave.end_date)
                    for yr, mo in sorted(months_to_recalc):
                        _salary_engine.process_salary_calculation(leave.employee_id, yr, mo)
                    result["salary_recalculated"] = True
                except Exception as e:
                    result["salary_warning"] = "薪資重算失敗，請手動前往薪資頁面重新計算"
                    logger.error("請假修改退審後薪資重算失敗：%s", e)

        return result
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/leaves/{leave_id}")
def delete_leave(leave_id: int, current_user: dict = Depends(require_staff_permission(Permission.LEAVES_WRITE))):
    """刪除請假記錄"""
    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail=LEAVE_RECORD_NOT_FOUND)

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
        raise_safe_500(e)
    finally:
        session.close()


class ApproveRequest(BaseModel):
    approved: bool
    rejection_reason: Optional[str] = None
    force_without_substitute: bool = False


class LeaveBatchApproveRequest(BaseModel):
    ids: List[int]
    approved: bool
    rejection_reason: Optional[str] = None


@router.put("/leaves/{leave_id}/approve")
def approve_leave(
    leave_id: int,
    data: ApproveRequest,
    current_user: dict = Depends(require_staff_permission(Permission.LEAVES_WRITE)),
):
    """核准/駁回請假。駁回時 rejection_reason 為必填。"""
    if not data.approved and not (data.rejection_reason or "").strip():
        raise HTTPException(status_code=400, detail="駁回時必須填寫原因")
    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).with_for_update().first()
        if not leave:
            raise HTTPException(status_code=404, detail=LEAVE_RECORD_NOT_FOUND)

        # ── 自我核准防護 ─────────────────────────────────────────────────────────
        # 僅在 approver 確實擁有 employee_id 且與申請人相同時才拒絕。
        # 無 employee_id 的帳號（如純管理員）本身無法提出假單，不構成自我核准風險。
        approver_eid = current_user.get("employee_id")
        if approver_eid and leave.employee_id == approver_eid:
            raise HTTPException(status_code=403, detail="不可自我核准請假單")

        # ── 角色資格檢查 ────────────────────────────────────────────────────────
        submitter_role = _get_submitter_role(leave.employee_id, session)
        approver_role = current_user.get("role", "")
        if not _check_approval_eligibility("leave", submitter_role, approver_role, session):
            raise HTTPException(
                status_code=403,
                detail=f"您的角色（{approver_role}）無權審核此員工（{submitter_role}）的請假申請",
            )

        warning = None
        if data.approved:
            if requires_supporting_document(leave.start_date, leave.end_date) and not _parse_paths(leave.attachment_paths):
                raise HTTPException(
                    status_code=400,
                    detail="請假超過 2 天需檢附證明附件後才能核准",
                )
            # ── 代理人序列式守衛 ──────────────────────────────────────────────
            _check_substitute_guard(leave, allow_without_substitute=data.force_without_substitute)
            if not data.force_without_substitute:
                _check_substitute_leave_conflict(
                    session,
                    leave.substitute_employee_id,
                    leave.start_date,
                    leave.end_date,
                    leave.start_time,
                    leave.end_time,
                )

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
            # 使用 include_pending=True + exclude_id=leave_id：
            # 計算「所有已核准 + 其他待審（排除本張）+ 本次時數」是否超出年度配額。
            # 此策略防止主管同時批准多張待審假單造成配額超支（concurrent approval race）。
            _check_leave_limits(
                session, leave.employee_id, leave.leave_type,
                leave.start_date, leave.leave_hours,
                include_pending=True, exclude_id=leave_id,
            )
            _check_quota(
                session, leave.employee_id, leave.leave_type,
                leave.start_date.year, leave.leave_hours,
                include_pending=True, exclude_id=leave_id,
            )

            # ── 封存月薪保護（commit 前）────────────────────────────────────────
            # 核准假單會觸發薪資重算；若該月薪資已封存，必須在 commit 前阻擋，
            # 否則假單被核准、薪資沒更新，DB 永遠處於矛盾狀態。
            _check_salary_months_not_finalized(
                session, leave.employee_id,
                {(leave.start_date.year, leave.start_date.month)},
            )

        # ── V8：防禦性扣款比例同步 ───────────────────────────────────────────
        # 核准時確保 deduction_ratio 與 is_deductible 已正確設定。
        # 場景：假單經多次編輯後，deduction_ratio 可能為 None 或與假別不符。
        # 只補全 None；若已有明確值（含自訂覆蓋）則保留，僅記錄警告。
        if data.approved and leave.leave_type in LEAVE_DEDUCTION_RULES:
            standard_ratio = LEAVE_DEDUCTION_RULES[leave.leave_type]
            if leave.deduction_ratio is None:
                leave.deduction_ratio = standard_ratio
                leave.is_deductible = standard_ratio > 0
                logger.info(
                    "核准假單 #%d 時補全缺失的 deduction_ratio：%s → %.2f",
                    leave_id, leave.leave_type, standard_ratio,
                )
            elif leave.deduction_ratio != standard_ratio:
                logger.warning(
                    "核准假單 #%d 發現自訂扣款比例：%s 標準值=%.2f，實際值=%.2f（admin 自訂）",
                    leave_id, leave.leave_type, standard_ratio, leave.deduction_ratio,
                )
            leave.is_deductible = (leave.deduction_ratio or 0) > 0

        leave.is_approved = data.approved
        leave.approved_by = current_user.get("username", "管理員") if data.approved else None
        leave.rejection_reason = data.rejection_reason.strip() if not data.approved and data.rejection_reason else None
        if data.approved and data.force_without_substitute:
            leave.substitute_status = "waived"

        action = "approved" if data.approved else "rejected"
        approval_comment = data.rejection_reason if not data.approved else None
        if data.approved and data.force_without_substitute:
            approval_comment = "主管警告後核准：未取得代理人接受"
        _write_approval_log("leave", leave_id, action, current_user,
                            approval_comment, session)
        session.commit()

        result = {"message": "已核准" if data.approved else "已駁回"}
        if warning:
            result["warning"] = warning

        # 個人 LINE 推播（審核結果）
        if _line_service is not None:
            try:
                emp_user = session.query(User).filter(User.employee_id == leave.employee_id).first()
                if emp_user and emp_user.line_user_id:
                    emp = session.query(Employee).filter(Employee.id == leave.employee_id).first()
                    emp_name = emp.name if emp else "員工"
                    _line_service.notify_leave_result(
                        emp_user.line_user_id, emp_name,
                        leave.leave_type, leave.start_date, leave.end_date,
                        data.approved, data.rejection_reason,
                    )
            except Exception as _le:
                logger.warning("假單審核 LINE 推播失敗: %s", _le)

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


@router.post("/leaves/batch-approve")
def batch_approve_leaves(
    data: LeaveBatchApproveRequest,
    current_user: dict = Depends(require_staff_permission(Permission.LEAVES_WRITE)),
):
    """批次核准/駁回請假。兩階段原子提交：先全部驗證，再統一 commit。"""
    if not data.approved and not (data.rejection_reason or "").strip():
        raise HTTPException(status_code=400, detail="批次駁回時必須填寫原因")

    succeeded = []
    failed = []

    session = get_session()
    try:
        # ── 預先批次載入，避免 N+1 ────────────────────────────────────────
        leave_map = {
            lv.id: lv
            for lv in session.query(LeaveRecord)
            .filter(LeaveRecord.id.in_(data.ids))
            .with_for_update()
            .all()
        }
        emp_ids = {lv.employee_id for lv in leave_map.values()}
        submitter_role_map: dict[int, str] = {
            u.employee_id: u.role
            for u in session.query(User.employee_id, User.role)
            .filter(User.employee_id.in_(emp_ids), User.is_active == True)
            .all()
        } if emp_ids else {}
        approver_role = current_user.get("role", "")
        _eligibility_cache: dict[str, bool] = {}

        # ── Phase 1：驗證 + 準備所有異動（不 commit）────────────────────────
        changes = []  # list of (leave_id, leave)
        for leave_id in data.ids:
            try:
                leave = leave_map.get(leave_id)
                if not leave:
                    failed.append({"id": leave_id, "reason": LEAVE_RECORD_NOT_FOUND})
                    continue

                # 防止自我核准
                approver_eid = current_user.get("employee_id")
                if approver_eid and leave.employee_id == approver_eid:
                    failed.append({"id": leave_id, "reason": "不可自我核准"})
                    continue

                # 角色資格檢查（403 視為失敗條目，不中斷批次）
                submitter_role = submitter_role_map.get(leave.employee_id, "teacher")
                if submitter_role not in _eligibility_cache:
                    _eligibility_cache[submitter_role] = _check_approval_eligibility(
                        "leave", submitter_role, approver_role, session
                    )
                if not _eligibility_cache[submitter_role]:
                    failed.append({
                        "id": leave_id,
                        "reason": f"您的角色（{approver_role}）無權審核此員工（{submitter_role}）的請假申請",
                    })
                    continue

                if data.approved:
                    try:
                        if requires_supporting_document(leave.start_date, leave.end_date) and not _parse_paths(leave.attachment_paths):
                            raise HTTPException(status_code=400, detail="請假超過 2 天需檢附證明附件後才能核准")
                        # 代理人序列式守衛
                        _check_substitute_guard(leave)
                        _check_substitute_leave_conflict(
                            session,
                            leave.substitute_employee_id,
                            leave.start_date,
                            leave.end_date,
                            leave.start_time,
                            leave.end_time,
                        )
                        _check_leave_limits(
                            session, leave.employee_id, leave.leave_type,
                            leave.start_date, leave.leave_hours,
                            include_pending=True, exclude_id=leave_id,
                        )
                        _check_quota(
                            session, leave.employee_id, leave.leave_type,
                            leave.start_date.year, leave.leave_hours,
                            include_pending=True, exclude_id=leave_id,
                        )
                        _check_salary_months_not_finalized(
                            session, leave.employee_id,
                            {(leave.start_date.year, leave.start_date.month)},
                        )
                    except HTTPException as e:
                        failed.append({"id": leave_id, "reason": e.detail})
                        continue

                # V8：批次核准時同步補全 deduction_ratio
                if data.approved and leave.leave_type in LEAVE_DEDUCTION_RULES:
                    standard_ratio = LEAVE_DEDUCTION_RULES[leave.leave_type]
                    if leave.deduction_ratio is None:
                        leave.deduction_ratio = standard_ratio
                        leave.is_deductible = standard_ratio > 0
                    leave.is_deductible = (leave.deduction_ratio or 0) > 0

                leave.is_approved = data.approved
                leave.approved_by = current_user.get("username", "管理員") if data.approved else None
                leave.rejection_reason = (
                    data.rejection_reason.strip()
                    if not data.approved and data.rejection_reason
                    else None
                )
                action = "approved" if data.approved else "rejected"
                _write_approval_log("leave", leave_id, action, current_user,
                                    data.rejection_reason if not data.approved else None, session)
                changes.append((leave_id, leave))
            except Exception as e:
                session.rollback()
                failed.append({"id": leave_id, "reason": str(e)})
                # 驗證階段失敗需清除 session 狀態，重新載入後續記錄
                session.expire_all()

        # ── Phase 2：所有驗證通過後統一 commit ──────────────────────────────
        if changes:
            try:
                session.commit()

                # 批次預載 LINE 通知所需的 User 與 Employee（1+1 查詢取代 2N）
                _line_user_map: dict = {}
                _emp_name_map: dict = {}
                if _line_service is not None:
                    change_emp_ids = list({lv.employee_id for _, lv in changes})
                    for u in session.query(User).filter(User.employee_id.in_(change_emp_ids)).all():
                        _line_user_map[u.employee_id] = u
                    for e in session.query(Employee.id, Employee.name).filter(Employee.id.in_(change_emp_ids)).all():
                        _emp_name_map[e.id] = e.name

                for leave_id, leave in changes:
                    succeeded.append(leave_id)

                    # 個人 LINE 推播（審核結果）
                    if _line_service is not None:
                        try:
                            emp_user = _line_user_map.get(leave.employee_id)
                            if emp_user and emp_user.line_user_id:
                                emp_name = _emp_name_map.get(leave.employee_id, "員工")
                                _line_service.notify_leave_result(
                                    emp_user.line_user_id, emp_name,
                                    leave.leave_type, leave.start_date, leave.end_date,
                                    data.approved, data.rejection_reason,
                                )
                        except Exception as _le:
                            logger.warning("批次假單審核 LINE 推播失敗（#%d）: %s", leave_id, _le)

                    if data.approved and _salary_engine is not None:
                        try:
                            emp_id = leave.employee_id
                            months: set = set()
                            cur = date(leave.start_date.year, leave.start_date.month, 1)
                            end_m = date(leave.end_date.year, leave.end_date.month, 1)
                            while cur <= end_m:
                                months.add((cur.year, cur.month))
                                cur = (
                                    date(cur.year + 1, 1, 1) if cur.month == 12
                                    else date(cur.year, cur.month + 1, 1)
                                )
                            for yr, mo in sorted(months):
                                _salary_engine.process_salary_calculation(emp_id, yr, mo)
                        except Exception as se:
                            logger.error("批次審核後薪資重算失敗（假單 #%d）：%s", leave_id, se)
            except Exception as e:
                session.rollback()
                for leave_id, _ in changes:
                    failed.append({"id": leave_id, "reason": f"統一提交失敗：{e}"})
    finally:
        session.close()

    return {"succeeded": succeeded, "failed": failed}




_LV_HEADER_FONT = Font(bold=True, size=11, color="FFFFFF")
_LV_HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
_LV_THIN_BORDER = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
)
_LV_CENTER_ALIGN = Alignment(horizontal="center")


def _lv_write_header(ws, row, headers):
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=col, value=h)
        cell.font = _LV_HEADER_FONT
        cell.fill = _LV_HEADER_FILL
        cell.border = _LV_THIN_BORDER
        cell.alignment = _LV_CENTER_ALIGN


@router.get("/leaves/import-template")
def get_leave_import_template(
    current_user: dict = Depends(require_staff_permission(Permission.LEAVES_WRITE)),
):
    """下載請假批次匯入 Excel 範本"""
    wb = Workbook()
    ws = wb.active
    ws.title = "請假匯入範本"

    headers = ["員工編號", "員工姓名", "假別代碼", "開始日期", "結束日期", "時數(可空)", "原因(可空)"]
    _lv_write_header(ws, 1, headers)

    ws.cell(row=2, column=1, value="E001")
    ws.cell(row=2, column=2, value="王小明")
    ws.cell(row=2, column=3, value="annual")
    ws.cell(row=2, column=4, value="2026-03-15")
    ws.cell(row=2, column=5, value="2026-03-15")
    ws.cell(row=2, column=6, value=8)
    ws.cell(row=2, column=7, value="年度特休")

    ws2 = wb.create_sheet("假別代碼說明")
    ws2.cell(row=1, column=1, value="假別代碼")
    ws2.cell(row=1, column=2, value="中文名稱")
    for idx, (code, label) in enumerate(LEAVE_TYPE_LABELS.items(), 2):
        ws2.cell(row=idx, column=1, value=code)
        ws2.cell(row=idx, column=2, value=label)

    return xlsx_streaming_response(wb, "請假匯入範本.xlsx")


@router.post("/leaves/import")
async def import_leaves(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_staff_permission(Permission.LEAVES_WRITE)),
):
    """批次匯入請假申請（建立草稿假單，is_approved=None，需後續人工審核）"""
    content = await read_upload_with_size_check(file)
    validate_file_signature(content, ".xlsx")
    try:
        df = pd.read_excel(BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"無法解析 Excel 檔案：{e}")

    label_to_code = {v: k for k, v in LEAVE_TYPE_LABELS.items()}

    results: dict = {"total": 0, "created": 0, "failed": 0, "errors": []}
    session = get_session()
    try:
        emp_by_id, emp_by_name = build_employee_lookup(session)

        for idx, row in df.iterrows():
            results["total"] += 1
            row_num = int(idx) + 2
            try:
                emp = resolve_employee_from_row(row, emp_by_id, emp_by_name)

                leave_type_raw = str(row.get("假別代碼", "")).strip()
                if leave_type_raw in LEAVE_TYPE_LABELS:
                    leave_type = leave_type_raw
                elif leave_type_raw in label_to_code:
                    leave_type = label_to_code[leave_type_raw]
                else:
                    raise ValueError(f"無效的假別代碼：{leave_type_raw}（請參考「假別代碼說明」頁）")

                start_raw = row.get("開始日期")
                end_raw = row.get("結束日期")
                if pd.isna(start_raw) or pd.isna(end_raw):
                    raise ValueError("開始日期或結束日期不得為空")
                try:
                    start_date = pd.to_datetime(start_raw).date()
                    end_date = pd.to_datetime(end_raw).date()
                except Exception:
                    raise ValueError("日期格式錯誤，建議使用 YYYY-MM-DD")

                if end_date < start_date:
                    raise ValueError("結束日期不得早於開始日期")
                if start_date.year != end_date.year or start_date.month != end_date.month:
                    raise ValueError("請假區間不可跨月，請拆成多筆分別匯入")

                hours_raw = row.get("時數(可空)")
                if hours_raw is None or pd.isna(hours_raw):
                    leave_hours = 8.0
                else:
                    try:
                        leave_hours = float(hours_raw)
                        if leave_hours < 0.5:
                            raise ValueError("時數至少 0.5 小時")
                    except (ValueError, TypeError):
                        leave_hours = 8.0

                reason_raw = row.get("原因(可空)")
                reason = str(reason_raw).strip() if reason_raw is not None and not pd.isna(reason_raw) else None

                effective_ratio = LEAVE_DEDUCTION_RULES[leave_type]
                leave = LeaveRecord(
                    employee_id=emp.id,
                    leave_type=leave_type,
                    start_date=start_date,
                    end_date=end_date,
                    leave_hours=leave_hours,
                    is_deductible=effective_ratio > 0,
                    deduction_ratio=effective_ratio,
                    reason=reason,
                    is_approved=None,
                )
                session.add(leave)
                session.flush()
                results["created"] += 1
            except Exception as e:
                results["failed"] += 1
                results["errors"].append(f"第 {row_num} 行: {str(e)}")

        session.commit()
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="匯入失敗")
    finally:
        session.close()

    return results


@router.get("/leaves/{leave_id}/attachments/{filename}")
def get_leave_attachment(
    leave_id: int,
    filename: str,
    current_user: dict = Depends(require_staff_permission(Permission.LEAVES_READ)),
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
