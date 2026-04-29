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
from utils.excel_utils import SafeWorksheet
from utils.rate_limit import SlidingWindowLimiter

# 批次核准為重 DB 操作；每分鐘 10 次緩衝正常工作量，封住批次濫用
_batch_approve_limiter = SlidingWindowLimiter(
    max_calls=10,
    window_seconds=60,
    name="leave_batch_approve",
    error_detail="批次審核操作過於頻繁，請稍後再試",
).as_dependency()
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from utils.leave_validators import validate_leave_hours_value, validate_leave_date_order
from sqlalchemy import or_, and_

from models.database import (
    get_session,
    Employee,
    LeaveRecord,
    LeaveQuota,
    OvertimeRecord,
    SalaryRecord,
    User,
)
from utils.auth import require_staff_permission
from utils.error_messages import EMPLOYEE_DOES_NOT_EXIST, LEAVE_RECORD_NOT_FOUND
from utils.permissions import Permission
from utils.approval_helpers import (
    _get_submitter_role,
    _check_approval_eligibility,
    _write_approval_log,
)
from utils.excel_utils import xlsx_streaming_response
from utils.import_utils import build_employee_lookup, resolve_employee_from_row
from utils.file_upload import read_upload_with_size_check, validate_file_signature
from utils.storage import get_storage_path
from api.leaves_quota import (
    quota_router,
    LEAVE_TYPE_LABELS,
    LEAVE_DEDUCTION_RULES,
    _check_leave_limits,
    _check_quota,
    _check_compensatory_quota,
    _get_sick_committed_hours,
    assert_sick_leave_within_statutory_caps,
)
from api.leaves_workday import workday_router, validate_leave_hours_against_schedule
from services.leave_policy import requires_supporting_document

_UPLOAD_MODULE = "leave_attachments"


def _upload_base() -> Path:
    return get_storage_path(_UPLOAD_MODULE)


def _parse_paths(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _safe_attach_path(leave_id: int, filename: str) -> Path:
    """解析附件路徑並確認落在 upload base 之內（路徑穿越防護）。"""
    base = _upload_base()
    resolved = (base / str(leave_id) / filename).resolve()
    try:
        resolved.relative_to(base.resolve())
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
    record = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == employee_id,
            SalaryRecord.is_finalized == True,
            or_(
                *(
                    and_(
                        SalaryRecord.salary_year == yr, SalaryRecord.salary_month == mo
                    )
                    for yr, mo in months
                )
            ),
        )
        .first()
    )
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


def _guard_leave_quota(
    session,
    employee_id: int,
    leave_type: str,
    year: int,
    leave_hours: float,
    is_hospitalized: bool,
    exclude_id: int = None,
    include_pending: bool = True,
) -> None:
    """sick 走勞工請假規則第 4 條雙配額；compensatory 走補休專用配額（quota 不存在=0）；
    其他假別走 _check_quota 單一配額。"""
    if leave_type == "sick":
        from datetime import date as _date  # local re-import safe

        out_used = _get_sick_committed_hours(
            session, employee_id, year, is_hospitalized=False, exclude_id=exclude_id
        )
        hosp_used = _get_sick_committed_hours(
            session, employee_id, year, is_hospitalized=True, exclude_id=exclude_id
        )
        assert_sick_leave_within_statutory_caps(
            out_used, hosp_used, leave_hours, bool(is_hospitalized)
        )
    elif leave_type == "compensatory":
        _check_compensatory_quota(
            session,
            employee_id,
            year,
            leave_hours,
            exclude_id=exclude_id,
            include_pending=include_pending,
        )
    else:
        _check_quota(
            session,
            employee_id,
            leave_type,
            year,
            leave_hours,
            include_pending=include_pending,
            exclude_id=exclude_id,
        )


def _apply_leave_update_and_revoke(leave, data, current_user, leave_id: int) -> None:
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
            leave_id,
            leave.employee_id,
            leave.start_date,
            leave.end_date,
            leave.leave_type,
            current_user.get("username", "unknown"),
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
    is_hospitalized: bool = False  # 病假住院旗標（勞工請假規則第 4 條）
    deduction_ratio: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description="扣薪比例覆蓋（不提供則依假別預設值，0.0=全薪，1.0=全扣）",
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
    is_hospitalized: Optional[bool] = None
    deduction_ratio: Optional[float] = Field(
        None,
        ge=0.0,
        le=1.0,
        description="扣薪比例覆蓋（不提供則依假別預設值，0.0=全薪，1.0=全扣）",
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
        if (
            self.start_date
            and self.end_date
            and (
                self.start_date.year != self.end_date.year
                or self.start_date.month != self.end_date.month
            )
        ):
            raise ValueError("請假區間不可跨月，若需跨越月底請拆成兩張假單分別申請")
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
        q = q.filter(
            or_(LeaveRecord.is_approved == True, LeaveRecord.is_approved.is_(None))
        )
    else:
        q = q.filter(LeaveRecord.is_approved == True)
    if exclude_id is not None:
        q = q.filter(LeaveRecord.id != exclude_id)

    is_new_single_day = start_date == end_date

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
    """代理人不可在相同時段有待審或已核准的請假或加班記錄（V14）。

    F-005：detail 採 generic 訊息，不再揭露代理人的請假/加班區間與審核狀態。
    舊實作回傳「代理人在 {start_date} ~ {end_date} 已有{待審核|已核准}請假記錄」
    讓員工 A 可用 substitute_employee_id=B + 不同日期反覆探測 B 的請假/加班行事曆
    （二分搜尋還原整段排程）。改為單一中性訊息後，攻擊者無法從 409 detail 推回
    代理人的具體日期或審批狀態。管理端（api/leaves.py 核准路徑）也會走此 helper，
    一併收斂訊息（避免維護兩套 detail）。
    """
    if substitute_employee_id is None:
        return

    _GENERIC_DETAIL = "代理人於該期間已有其他請假/加班，無法擔任代理人，請改選其他人選"

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
        raise HTTPException(status_code=409, detail=_GENERIC_DETAIL)

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
        raise HTTPException(status_code=409, detail=_GENERIC_DETAIL)


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
            q = q.filter(
                LeaveRecord.start_date >= date(year, 1, 1),
                LeaveRecord.start_date <= date(year, 12, 31),
            )

        records = q.order_by(LeaveRecord.start_date.desc()).all()

        # 預先載入員工角色映射（減少逐筆查 DB 的 N+1 問題）
        from models.database import User as UserModel

        employee_ids = list({leave.employee_id for leave, _ in records})
        user_roles = {}
        if employee_ids:
            users = (
                session.query(UserModel)
                .filter(
                    UserModel.employee_id.in_(employee_ids),
                    UserModel.is_active == True,
                )
                .all()
            )
            user_roles = {u.employee_id: u.role for u in users}

        # 預先載入代理人姓名（批次查詢，避免 N+1）
        substitute_ids = list(
            {
                leave.substitute_employee_id
                for leave, _ in records
                if leave.substitute_employee_id
            }
        )
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
            all_swaps = (
                session.query(ShiftSwapRequest)
                .filter(
                    ShiftSwapRequest.requester_id.in_(involved_emp_ids),
                    ShiftSwapRequest.status.in_(["pending", "accepted"]),
                    ShiftSwapRequest.swap_date >= _swap_range_start,
                    ShiftSwapRequest.swap_date <= _swap_range_end,
                )
                .all()
            )
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

            results.append(
                {
                    "id": leave.id,
                    "employee_id": leave.employee_id,
                    "employee_name": emp.name,
                    "submitter_role": user_roles.get(leave.employee_id, "teacher"),
                    "leave_type": leave.leave_type,
                    "leave_type_label": LEAVE_TYPE_LABELS.get(
                        leave.leave_type, leave.leave_type
                    ),
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
                    "substitute_employee_name": substitute_names.get(
                        leave.substitute_employee_id
                    ),
                    "substitute_status": leave.substitute_status or "not_required",
                    "substitute_responded_at": (
                        leave.substitute_responded_at.isoformat()
                        if leave.substitute_responded_at
                        else None
                    ),
                    "related_swap": related_swap,
                    "created_at": (
                        leave.created_at.isoformat() if leave.created_at else None
                    ),
                }
            )
        return results
    finally:
        session.close()


# ── 請假記錄 CRUD ──────────────────────────────────────────────


@router.post("/leaves", status_code=201)
def create_leave(
    data: LeaveCreate,
    current_user: dict = Depends(require_staff_permission(Permission.LEAVES_WRITE)),
):
    """新增請假記錄"""
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == data.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail=EMPLOYEE_DOES_NOT_EXIST)

        overlap = _check_overlap(
            session,
            data.employee_id,
            data.start_date,
            data.end_date,
            data.start_time,
            data.end_time,
        )
        if overlap:
            raise HTTPException(
                status_code=409,
                detail=f"該員工在 {overlap.start_date} ~ {overlap.end_date} 已有已核准的請假記錄（ID: {overlap.id}），無法重複請假",
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
            session,
            data.employee_id,
            data.leave_type,
            data.start_date,
            data.leave_hours,
        )
        _guard_leave_quota(
            session,
            data.employee_id,
            data.leave_type,
            data.start_date.year,
            data.leave_hours,
            bool(data.is_hospitalized),
        )

        # 優先使用 API 傳入的覆蓋值；未提供則依假別預設規則
        effective_ratio = (
            data.deduction_ratio
            if data.deduction_ratio is not None
            else LEAVE_DEDUCTION_RULES[data.leave_type]
        )
        if data.deduction_ratio is not None:
            default_ratio = LEAVE_DEDUCTION_RULES.get(data.leave_type, -1)
            logger.warning(
                "假單扣薪比例被手動覆蓋 by user=%s employee_id=%s leave_type=%s "
                "default_ratio=%s overridden_ratio=%s",
                current_user.get("username"),
                data.employee_id,
                data.leave_type,
                default_ratio,
                data.deduction_ratio,
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
            is_hospitalized=(
                bool(data.is_hospitalized) if data.leave_type == "sick" else False
            ),
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
def update_leave(
    leave_id: int,
    data: LeaveUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.LEAVES_WRITE)),
):
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
        new_start_time = (
            data.start_time if data.start_time is not None else leave.start_time
        )
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
            session,
            leave.employee_id,
            new_start,
            new_end,
            new_start_time,
            new_end_time,
            exclude_id=leave_id,
        )
        if overlap:
            raise HTTPException(
                status_code=409,
                detail=f"修改後的日期與已核准的請假記錄重疊（{overlap.start_date} ~ {overlap.end_date}，ID: {overlap.id}）",
            )

        new_type = data.leave_type or leave.leave_type
        new_hours = (
            data.leave_hours if data.leave_hours is not None else leave.leave_hours
        )
        validate_leave_hours_against_schedule(
            session,
            leave.employee_id,
            new_start,
            new_end,
            new_hours,
            new_start_time,
            new_end_time,
        )
        # 已核准的假單退審後視同重新提交：重新過一次配額（排除自身）
        _check_leave_limits(
            session,
            leave.employee_id,
            new_type,
            new_start,
            new_hours,
            exclude_id=leave_id,
        )
        new_is_hosp = (
            data.is_hospitalized
            if data.is_hospitalized is not None
            else leave.is_hospitalized
        )
        _guard_leave_quota(
            session,
            leave.employee_id,
            new_type,
            new_start.year,
            new_hours,
            new_is_hosp,
            exclude_id=leave_id,
        )

        # 封存月薪保護：同時檢查原始月份與更新後月份
        if was_approved:
            _check_salary_months_not_finalized(
                session,
                leave.employee_id,
                {orig_month, (new_start.year, new_start.month)},
            )

        _apply_leave_update_and_revoke(leave, data, current_user, leave_id)
        session.commit()

        result = {"message": "請假記錄已更新"}
        if was_approved:
            result["message"] += "；原核准狀態已自動退回「待審核」，請重新送審"
            result["reset_to_pending"] = True
            if _salary_engine is not None:
                # 跨月修改時,新區間只涵蓋新月份；舊月份的扣款須一併撤銷重算,
                # 否則 orig_month 的舊扣款仍存在於該月薪資,與新月份合計形成重複扣薪。
                months_to_recalc = _collect_leave_months(
                    leave.start_date, leave.end_date
                )
                months_to_recalc.add(orig_month)
                try:
                    for yr, mo in sorted(months_to_recalc):
                        _salary_engine.process_salary_calculation(
                            leave.employee_id, yr, mo
                        )
                    result["salary_recalculated"] = True
                except Exception as e:
                    result["salary_warning"] = (
                        "薪資重算失敗，請手動前往薪資頁面重新計算"
                    )
                    logger.error("請假修改退審後薪資重算失敗：%s", e)
                    # 重算失敗 → 標 stale,避免後續 finalize 把「假單已退審但薪資未更新」
                    # 的舊資料封存。對齊 approve_leave 降級樣板。
                    from services.salary.utils import mark_salary_stale

                    for yr, mo in sorted(months_to_recalc):
                        try:
                            mark_salary_stale(session, leave.employee_id, yr, mo)
                        except Exception:
                            logger.warning(
                                "請假修改降級時標記 SalaryRecord stale 失敗 emp=%d %d/%d",
                                leave.employee_id,
                                yr,
                                mo,
                                exc_info=True,
                            )
                    try:
                        session.commit()
                    except Exception:
                        logger.warning(
                            "請假修改降級時 commit stale 標記失敗", exc_info=True
                        )
                        session.rollback()

        return result
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/leaves/{leave_id}")
def delete_leave(
    leave_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.LEAVES_WRITE)),
):
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
                result["salary_warning"] = (
                    "假單已刪除，但薪資重算失敗，請手動前往薪資頁面重新計算"
                )
                logger.error("刪除假單後薪資重算失敗：%s", e)
                # 重算失敗 → 標 stale,避免後續 finalize 在「假單已刪除但扣款未撤銷」
                # 的狀態下誤封存舊薪資。對齊 approve_leave 降級樣板。
                from services.salary.utils import mark_salary_stale

                try:
                    mark_salary_stale(session, emp_id, *leave_month)
                    session.commit()
                except Exception:
                    logger.warning(
                        "刪除假單降級時 commit stale 標記失敗 emp=%d %d/%d",
                        emp_id,
                        *leave_month,
                        exc_info=True,
                    )
                    session.rollback()

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
    # 同員工同時段已有其他已核准假單時，預設 409 阻擋；主管確認後可帶
    # force_overlap=True 強制過（記稽核日誌），對齊 force_without_substitute 模式。
    force_overlap: bool = False
    # force_overlap=True 時必填原因（≥10 字）；會寫進 ApprovalLog comment 與 logger，
    # 供日後稽核回溯為何允許重疊（避免重複扣薪/重複占配額被忽視）。
    force_overlap_reason: Optional[str] = Field(None, max_length=500)

    @model_validator(mode="after")
    def _force_overlap_requires_reason(self):
        if self.force_overlap:
            cleaned = (self.force_overlap_reason or "").strip()
            if len(cleaned) < 10:
                raise ValueError(
                    "force_overlap=True 時必須填寫 force_overlap_reason（至少 10 字）"
                )
            self.force_overlap_reason = cleaned
        return self


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
        leave = (
            session.query(LeaveRecord)
            .filter(LeaveRecord.id == leave_id)
            .with_for_update()
            .first()
        )
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
        if not _check_approval_eligibility(
            "leave", submitter_role, approver_role, session
        ):
            raise HTTPException(
                status_code=403,
                detail=f"您的角色（{approver_role}）無權審核此員工（{submitter_role}）的請假申請",
            )

        # 核准/駁回狀態是否實質變動；True→False、False→True、None→True 都屬於
        # 會影響 SalaryRecord 扣款的轉換，必須觸發封存守衛與薪資重算。
        was_approved = leave.is_approved is True
        approval_changed = was_approved != data.approved

        # ── 提早取得薪資鎖（封存守衛 + commit + 重算共用同一鎖窗）─────────────
        # Why: 原流程「_check_finalized → commit leave → process_salary_calculation」
        # 中間沒有 lock，finalize 可能在 commit 之後 / recalc 之前搶到鎖,結果
        # 假單變更已落地但薪資沒重算就被封存。改在 approval_changed 路徑一進來
        # 就 acquire per-emp salary lock(同 transaction 可重入,recalc 內再取一次
        # 不會 deadlock),保證守衛/commit/recalc 三步在同個鎖窗內完成。
        if approval_changed:
            from utils.advisory_lock import acquire_salary_lock as _acquire_salary_lock

            _months_for_lock: set[tuple[int, int]] = set()
            _cur = date(leave.start_date.year, leave.start_date.month, 1)
            _end = date(leave.end_date.year, leave.end_date.month, 1)
            while _cur <= _end:
                _months_for_lock.add((_cur.year, _cur.month))
                _cur = (
                    date(_cur.year + 1, 1, 1)
                    if _cur.month == 12
                    else date(_cur.year, _cur.month + 1, 1)
                )
            for _y, _m in sorted(_months_for_lock):
                _acquire_salary_lock(
                    session, employee_id=leave.employee_id, year=_y, month=_m
                )

        warning = None
        if data.approved:
            if requires_supporting_document(
                leave.start_date, leave.end_date
            ) and not _parse_paths(leave.attachment_paths):
                raise HTTPException(
                    status_code=400,
                    detail="請假超過 2 天需檢附證明附件後才能核准",
                )
            # ── 代理人序列式守衛 ──────────────────────────────────────────────
            _check_substitute_guard(
                leave, allow_without_substitute=data.force_without_substitute
            )
            if not data.force_without_substitute:
                _check_substitute_leave_conflict(
                    session,
                    leave.substitute_employee_id,
                    leave.start_date,
                    leave.end_date,
                    leave.start_time,
                    leave.end_time,
                )

            # 核准最後一道關卡：時數不得超過該區間排班工時。建立路徑（管理端 / portal）
            # 已檢查過，但匯入、舊資料或 DB 手改可能繞過，此處補一層 defense-in-depth。
            validate_leave_hours_against_schedule(
                session,
                leave.employee_id,
                leave.start_date,
                leave.end_date,
                leave.leave_hours,
                leave.start_time,
                leave.end_time,
            )

            # 重疊核准硬擋：同員工同時段已有其他已核准假單時，預設拒絕核准，
            # 避免重複扣薪或重複占配額。主管確認後仍要核准，需帶 force_overlap=True
            # 才會降級為 warning（會記稽核日誌）。
            conflict = _check_overlap(
                session,
                leave.employee_id,
                leave.start_date,
                leave.end_date,
                leave.start_time,
                leave.end_time,
                exclude_id=leave_id,
            )
            if conflict:
                if not data.force_overlap:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"該員工在 {conflict.start_date} ~ {conflict.end_date} "
                            f"已有另一筆已核准的請假（ID: {conflict.id}），"
                            "若確認仍要核准請帶 force_overlap=true 並填寫 force_overlap_reason"
                        ),
                    )
                # ── force_overlap 三重守衛 ─────────────────────────────────
                # 重疊放行可能造成同時段重複扣薪/重複占配額；不該只憑 force=True 即過：
                # 1. force_overlap_reason ≥10 字（schema 已驗）
                # 2. ACTIVITY_PAYMENT_APPROVE 簽核（避免一線主管獨自決定）
                # 3. conflict 詳情寫進 ApprovalLog comment（永久稽核軌跡，不只是 logger）
                from utils.finance_guards import has_finance_approve

                if not has_finance_approve(current_user):
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "force_overlap=True 需具備『金流簽核』權限"
                            "（ACTIVITY_PAYMENT_APPROVE），請改由具該權限者執行"
                        ),
                    )
                conflict_summary = (
                    f"主管警告後核准（force_overlap）：與假單 #{conflict.id} "
                    f"({conflict.start_date}~{conflict.end_date}) 重疊；"
                    f"原因：{data.force_overlap_reason}"
                )
                warning = conflict_summary
                logger.warning(
                    "假單 #%d 主管以 force_overlap=true 強制核准，"
                    "與已核准假單 #%d (%s~%s) 重疊；操作者：%s；原因：%s",
                    leave_id,
                    conflict.id,
                    conflict.start_date,
                    conflict.end_date,
                    current_user.get("username", "unknown"),
                    data.force_overlap_reason,
                )

            # ── 配額硬檢查（核准動作）──────────────────────────────────────────
            # 使用 include_pending=True + exclude_id=leave_id：
            # 計算「所有已核准 + 其他待審（排除本張）+ 本次時數」是否超出年度配額。
            # 此策略防止主管同時批准多張待審假單造成配額超支（concurrent approval race）。
            _check_leave_limits(
                session,
                leave.employee_id,
                leave.leave_type,
                leave.start_date,
                leave.leave_hours,
                include_pending=True,
                exclude_id=leave_id,
            )
            _guard_leave_quota(
                session,
                leave.employee_id,
                leave.leave_type,
                leave.start_date.year,
                leave.leave_hours,
                bool(leave.is_hospitalized),
                exclude_id=leave_id,
                include_pending=True,
            )

        # ── 封存月薪保護（commit 前）────────────────────────────────────────
        # 核准假單或將已核准假單改為駁回都會改變 SalaryRecord；若該月薪資已封存，
        # 必須在 commit 前阻擋，否則假單狀態被翻面、薪資沒更新，DB 永遠處於矛盾狀態。
        if approval_changed:
            _check_salary_months_not_finalized(
                session,
                leave.employee_id,
                {(leave.start_date.year, leave.start_date.month)},
            )

        # ── V8：防禦性扣款比例同步 ───────────────────────────────────────────
        # 核准時強制確保 deduction_ratio 與假別標準一致，避免「假別變更後
        # ratio 未重設」造成薪資漏扣/誤扣。若 HR 需要自訂比例，應在核准前
        # 以假單編輯入口明確傳入 deduction_ratio（該路徑會設置 is_deductible
        # 並記錄來源）。
        if data.approved and leave.leave_type in LEAVE_DEDUCTION_RULES:
            standard_ratio = LEAVE_DEDUCTION_RULES[leave.leave_type]
            if leave.deduction_ratio is None:
                leave.deduction_ratio = standard_ratio
                logger.info(
                    "核准假單 #%d 時補全缺失的 deduction_ratio：%s → %.2f",
                    leave_id,
                    leave.leave_type,
                    standard_ratio,
                )
            elif leave.deduction_ratio != standard_ratio:
                logger.warning(
                    "核准假單 #%d deduction_ratio 與假別標準不符：%s 標準=%.2f 實際=%.2f，強制改回標準值",
                    leave_id,
                    leave.leave_type,
                    standard_ratio,
                    leave.deduction_ratio,
                )
                leave.deduction_ratio = standard_ratio
            leave.is_deductible = (leave.deduction_ratio or 0) > 0

        leave.is_approved = data.approved
        leave.approved_by = (
            current_user.get("username", "管理員") if data.approved else None
        )
        leave.rejection_reason = (
            data.rejection_reason.strip()
            if not data.approved and data.rejection_reason
            else None
        )
        if data.approved and data.force_without_substitute:
            leave.substitute_status = "waived"

        action = "approved" if data.approved else "rejected"
        approval_comment = data.rejection_reason if not data.approved else None
        if data.approved and data.force_without_substitute:
            approval_comment = "主管警告後核准：未取得代理人接受"
        # 重疊放行：把 conflict 與原因寫進 ApprovalLog，留永久稽核痕跡
        if data.approved and warning:
            approval_comment = (
                f"{approval_comment}\n{warning}" if approval_comment else warning
            )
        _write_approval_log(
            "leave", leave_id, action, current_user, approval_comment, session
        )
        session.commit()

        result = {"message": "已核准" if data.approved else "已駁回"}
        if warning:
            result["warning"] = warning

        # 個人 LINE 推播（審核結果）
        if _line_service is not None:
            try:
                emp_user = (
                    session.query(User)
                    .filter(User.employee_id == leave.employee_id)
                    .first()
                )
                if emp_user and emp_user.line_user_id:
                    emp = (
                        session.query(Employee)
                        .filter(Employee.id == leave.employee_id)
                        .first()
                    )
                    emp_name = emp.name if emp else "員工"
                    _line_service.notify_leave_result(
                        emp_user.line_user_id,
                        emp_name,
                        leave.leave_type,
                        leave.start_date,
                        leave.end_date,
                        data.approved,
                        data.rejection_reason,
                    )
            except Exception as _le:
                logger.warning("假單審核 LINE 推播失敗: %s", _le)

        # 核准狀態變動（approve 或 reject-of-approved）後，自動重算該員工所有涉及月份的薪資
        if approval_changed and _salary_engine is not None:
            try:
                emp_id = leave.employee_id
                # 計算假單跨越的所有 (year, month)
                months_to_recalc = set()
                cur = date(leave.start_date.year, leave.start_date.month, 1)
                end = date(leave.end_date.year, leave.end_date.month, 1)
                while cur <= end:
                    months_to_recalc.add((cur.year, cur.month))
                    cur = (
                        date(cur.year + 1, 1, 1)
                        if cur.month == 12
                        else date(cur.year, cur.month + 1, 1)
                    )

                for year, month in sorted(months_to_recalc):
                    _salary_engine.process_salary_calculation(emp_id, year, month)
                    logger.info(
                        f"請假審核狀態變動後自動重算薪資：emp_id={emp_id}, {year}/{month}, approved={data.approved}"
                    )

                result["salary_recalculated"] = True
                if data.approved:
                    result["message"] = "已核准，薪資已自動重算"
                else:
                    result["message"] = "已駁回，薪資已自動重算"
            except Exception as e:
                result["salary_recalculated"] = False
                result["salary_warning"] = (
                    "操作成功，但薪資重算失敗，請手動前往薪資頁面重新計算"
                )
                logger.error(f"請假審核後薪資重算失敗：{e}")
                # 把所有應重算月份的 SalaryRecord 標 stale,避免後續 finalize
                # 在「假單已審核但薪資未更新」的狀態下誤封存舊薪資。
                from services.salary.utils import mark_salary_stale

                for year, month in sorted(months_to_recalc):
                    try:
                        mark_salary_stale(session, emp_id, year, month)
                    except Exception:
                        logger.warning(
                            "請假審核降級時標記 SalaryRecord stale 失敗 emp=%d %d/%d",
                            emp_id,
                            year,
                            month,
                            exc_info=True,
                        )
                try:
                    session.commit()
                except Exception:
                    logger.warning(
                        "請假審核降級時 commit stale 標記失敗", exc_info=True
                    )
                    session.rollback()

        return result
    finally:
        session.close()


@router.post("/leaves/batch-approve")
def batch_approve_leaves(
    data: LeaveBatchApproveRequest,
    _rl=Depends(_batch_approve_limiter),
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
        submitter_role_map: dict[int, str] = (
            {
                u.employee_id: u.role
                for u in session.query(User.employee_id, User.role)
                .filter(User.employee_id.in_(emp_ids), User.is_active == True)
                .all()
            }
            if emp_ids
            else {}
        )
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
                    failed.append(
                        {
                            "id": leave_id,
                            "reason": f"您的角色（{approver_role}）無權審核此員工（{submitter_role}）的請假申請",
                        }
                    )
                    continue

                # 核准/駁回狀態是否實質變動（影響 SalaryRecord）
                was_approved = leave.is_approved is True
                approval_changed = was_approved != data.approved

                if data.approved:
                    try:
                        if requires_supporting_document(
                            leave.start_date, leave.end_date
                        ) and not _parse_paths(leave.attachment_paths):
                            raise HTTPException(
                                status_code=400,
                                detail="請假超過 2 天需檢附證明附件後才能核准",
                            )
                        # 與單筆核准相同的防線：時數不得超過該區間排班工時，
                        # 防止 import、舊資料或 DB 手改繞過的超額假單進入薪資。
                        validate_leave_hours_against_schedule(
                            session,
                            leave.employee_id,
                            leave.start_date,
                            leave.end_date,
                            leave.leave_hours,
                            leave.start_time,
                            leave.end_time,
                        )
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
                            session,
                            leave.employee_id,
                            leave.leave_type,
                            leave.start_date,
                            leave.leave_hours,
                            include_pending=True,
                            exclude_id=leave_id,
                        )
                        _guard_leave_quota(
                            session,
                            leave.employee_id,
                            leave.leave_type,
                            leave.start_date.year,
                            leave.leave_hours,
                            bool(leave.is_hospitalized),
                            exclude_id=leave_id,
                            include_pending=True,
                        )
                        # 重疊核准硬擋：批次無 force_overlap 旗標，一律拒絕。
                        # 借助 SQLAlchemy autoflush，前一輪已 set is_approved=True
                        # 的記錄會在這個 _check_overlap 查詢時被視為已核准，
                        # 確保「同批兩張同員工同時段」的後者會被擋下。
                        conflict = _check_overlap(
                            session,
                            leave.employee_id,
                            leave.start_date,
                            leave.end_date,
                            leave.start_time,
                            leave.end_time,
                            exclude_id=leave_id,
                        )
                        if conflict:
                            raise HTTPException(
                                status_code=409,
                                detail=(
                                    f"與已核准假單 #{conflict.id}"
                                    f"（{conflict.start_date}~{conflict.end_date}）"
                                    "重疊；批次核准不支援強制過，請改用單筆核准並帶 force_overlap"
                                ),
                            )
                    except HTTPException as e:
                        failed.append({"id": leave_id, "reason": e.detail})
                        continue

                # 封存月薪保護：approve 或 reject-of-approved 都會改變 SalaryRecord
                if approval_changed:
                    try:
                        _check_salary_months_not_finalized(
                            session,
                            leave.employee_id,
                            {(leave.start_date.year, leave.start_date.month)},
                        )
                    except HTTPException as e:
                        failed.append({"id": leave_id, "reason": e.detail})
                        continue

                # V8：批次核准時強制同步 deduction_ratio 到假別標準值
                if data.approved and leave.leave_type in LEAVE_DEDUCTION_RULES:
                    standard_ratio = LEAVE_DEDUCTION_RULES[leave.leave_type]
                    if leave.deduction_ratio is None:
                        leave.deduction_ratio = standard_ratio
                    elif leave.deduction_ratio != standard_ratio:
                        logger.warning(
                            "批次核准假單 #%d deduction_ratio 與假別標準不符：%s 標準=%.2f 實際=%.2f，強制改回標準值",
                            leave_id,
                            leave.leave_type,
                            standard_ratio,
                            leave.deduction_ratio,
                        )
                        leave.deduction_ratio = standard_ratio
                    leave.is_deductible = (leave.deduction_ratio or 0) > 0

                leave.is_approved = data.approved
                leave.approved_by = (
                    current_user.get("username", "管理員") if data.approved else None
                )
                leave.rejection_reason = (
                    data.rejection_reason.strip()
                    if not data.approved and data.rejection_reason
                    else None
                )
                action = "approved" if data.approved else "rejected"
                _write_approval_log(
                    "leave",
                    leave_id,
                    action,
                    current_user,
                    data.rejection_reason if not data.approved else None,
                    session,
                )
                changes.append((leave_id, leave, approval_changed))
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
                    change_emp_ids = list({lv.employee_id for _, lv, _ in changes})
                    for u in (
                        session.query(User)
                        .filter(User.employee_id.in_(change_emp_ids))
                        .all()
                    ):
                        _line_user_map[u.employee_id] = u
                    for e in (
                        session.query(Employee.id, Employee.name)
                        .filter(Employee.id.in_(change_emp_ids))
                        .all()
                    ):
                        _emp_name_map[e.id] = e.name

                for leave_id, leave, approval_changed in changes:
                    succeeded.append(leave_id)

                    # 個人 LINE 推播（審核結果）
                    if _line_service is not None:
                        try:
                            emp_user = _line_user_map.get(leave.employee_id)
                            if emp_user and emp_user.line_user_id:
                                emp_name = _emp_name_map.get(leave.employee_id, "員工")
                                _line_service.notify_leave_result(
                                    emp_user.line_user_id,
                                    emp_name,
                                    leave.leave_type,
                                    leave.start_date,
                                    leave.end_date,
                                    data.approved,
                                    data.rejection_reason,
                                )
                        except Exception as _le:
                            logger.warning(
                                "批次假單審核 LINE 推播失敗（#%d）: %s", leave_id, _le
                            )

                    # approve 或 reject-of-approved 都需重算薪資
                    if approval_changed and _salary_engine is not None:
                        emp_id = leave.employee_id
                        months: set = set()
                        cur = date(leave.start_date.year, leave.start_date.month, 1)
                        end_m = date(leave.end_date.year, leave.end_date.month, 1)
                        while cur <= end_m:
                            months.add((cur.year, cur.month))
                            cur = (
                                date(cur.year + 1, 1, 1)
                                if cur.month == 12
                                else date(cur.year, cur.month + 1, 1)
                            )
                        try:
                            for yr, mo in sorted(months):
                                _salary_engine.process_salary_calculation(
                                    emp_id, yr, mo
                                )
                        except Exception as se:
                            logger.error(
                                "批次審核後薪資重算失敗（假單 #%d）：%s", leave_id, se
                            )
                            # 重算失敗 → 標 stale,避免後續 finalize 在「假單已審核
                            # 但薪資未更新」的狀態下誤封存舊薪資。
                            from services.salary.utils import mark_salary_stale

                            for yr, mo in sorted(months):
                                try:
                                    mark_salary_stale(session, emp_id, yr, mo)
                                except Exception:
                                    logger.warning(
                                        "批次審核降級時標記 SalaryRecord stale 失敗 emp=%d %d/%d",
                                        emp_id,
                                        yr,
                                        mo,
                                        exc_info=True,
                                    )
                            try:
                                session.commit()
                            except Exception:
                                logger.warning(
                                    "批次審核降級時 commit stale 標記失敗",
                                    exc_info=True,
                                )
                                session.rollback()
            except Exception as e:
                session.rollback()
                for leave_id, _, _ in changes:
                    failed.append({"id": leave_id, "reason": f"統一提交失敗：{e}"})
    finally:
        session.close()

    return {"succeeded": succeeded, "failed": failed}


_LV_HEADER_FONT = Font(bold=True, size=11, color="FFFFFF")
_LV_HEADER_FILL = PatternFill(
    start_color="4472C4", end_color="4472C4", fill_type="solid"
)
_LV_THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
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
    ws = SafeWorksheet(wb.active)
    ws.title = "請假匯入範本"

    headers = [
        "員工編號",
        "員工姓名",
        "假別代碼",
        "開始日期",
        "結束日期",
        "時數(可空)",
        "原因(可空)",
    ]
    _lv_write_header(ws, 1, headers)

    ws.cell(row=2, column=1, value="E001")
    ws.cell(row=2, column=2, value="王小明")
    ws.cell(row=2, column=3, value="annual")
    ws.cell(row=2, column=4, value="2026-03-15")
    ws.cell(row=2, column=5, value="2026-03-15")
    ws.cell(row=2, column=6, value=8)
    ws.cell(row=2, column=7, value="年度特休")

    ws2 = SafeWorksheet(wb.create_sheet("假別代碼說明"))
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
                    raise ValueError(
                        f"無效的假別代碼：{leave_type_raw}（請參考「假別代碼說明」頁）"
                    )

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
                if (
                    start_date.year != end_date.year
                    or start_date.month != end_date.month
                ):
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
                reason = (
                    str(reason_raw).strip()
                    if reason_raw is not None and not pd.isna(reason_raw)
                    else None
                )

                # 與管理端 / portal 建立假單相同：時數不得超過該區間排班工時，
                # 且不得超過假別年度配額（含待審）。匯入跳過會讓單日 100h 這種
                # 超額假單進入待審，主管核准後直接依超額時數扣薪。
                validate_leave_hours_against_schedule(
                    session,
                    emp.id,
                    start_date,
                    end_date,
                    leave_hours,
                )
                _check_leave_limits(
                    session,
                    emp.id,
                    leave_type,
                    start_date,
                    leave_hours,
                )
                # 補休配額由加班核准動態累積；_check_quota 對非 QUOTA_LEAVE_TYPES
                # 直接 return，會放過超額補休（HR 可用 Excel 大量匯入繞過）。
                if leave_type == "compensatory":
                    _check_compensatory_quota(
                        session,
                        emp.id,
                        start_date.year,
                        leave_hours,
                    )
                else:
                    _check_quota(
                        session,
                        emp.id,
                        leave_type,
                        start_date.year,
                        leave_hours,
                    )

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
