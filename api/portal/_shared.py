"""
Portal shared constants, Pydantic models, and helper functions.
"""

import logging
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Optional

from cachetools import TTLCache
from fastapi import HTTPException
from utils.masking import mask_bank_account
from pydantic import BaseModel, field_validator, model_validator
from utils.constants import LEAVE_TYPE_LABELS, OVERTIME_TYPE_LABELS, MAX_OVERTIME_HOURS
from utils.leave_validators import validate_leave_hours_value, validate_leave_date_order
from utils.validators import validate_hhmm_format

from sqlalchemy import or_

from models.database import (
    get_session,
    Employee,
    DailyShift,
    ShiftAssignment,
    Classroom,
    Student,
)
from utils.auth import get_current_user

# ShiftType 很少異動，使用 TTLCache 5 分鐘，避免每次請求全表查詢
_shift_type_cache: TTLCache = TTLCache(maxsize=2, ttl=300)

logger = logging.getLogger(__name__)

WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]

# ============ Pydantic Models ============


class LeaveCreatePortal(BaseModel):
    leave_type: str
    start_date: date
    end_date: date
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    leave_hours: float = 8
    reason: Optional[str] = None
    substitute_employee_id: Optional[int] = None
    # 補休假單關聯來源加班（leave_type='compensatory' 時建議填寫，確保駁回加班時能自動撤銷）
    source_overtime_id: Optional[int] = None

    @field_validator("leave_hours")
    @classmethod
    def validate_leave_hours(cls, v):
        return validate_leave_hours_value(v)

    @model_validator(mode="after")
    def validate_date_order(self):
        validate_leave_date_order(self.start_date, self.end_date)
        return self


class OvertimeCreatePortal(BaseModel):
    overtime_date: date
    overtime_type: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    hours: float
    reason: Optional[str] = None
    use_comp_leave: bool = False  # 以補休代替加班費（1:1 換算）

    @field_validator("overtime_type")
    @classmethod
    def validate_overtime_type(cls, v):
        if v not in OVERTIME_TYPE_LABELS:
            allowed = ", ".join(OVERTIME_TYPE_LABELS.keys())
            raise ValueError(f"無效的加班類型，允許值：{allowed}")
        return v

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, v):
        if v <= 0:
            raise ValueError("加班時數必須大於 0")
        if v > MAX_OVERTIME_HOURS:
            raise ValueError(f"單筆加班時數不得超過 {MAX_OVERTIME_HOURS:.0f} 小時")
        return v

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_time_format(cls, v):
        return validate_hhmm_format(v)

    @model_validator(mode="after")
    def validate_time_order(self):
        if self.start_time and self.end_time:
            if self.start_time >= self.end_time:
                raise ValueError("start_time 必須早於 end_time（不支援跨日加班）")
        return self


class AnomalyConfirm(BaseModel):
    action: str  # "use_pto" | "accept" | "dispute"
    remark: Optional[str] = None


_mask_bank_account = mask_bank_account


class ProfileUpdate(BaseModel):
    phone: Optional[str] = None
    address: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    bank_code: Optional[str] = None
    bank_account: Optional[str] = None
    bank_account_name: Optional[str] = None


class SwapRequestCreate(BaseModel):
    target_id: int
    swap_date: date
    reason: Optional[str] = None


class SwapRequestRespond(BaseModel):
    action: str  # "accept" | "reject"
    remark: Optional[str] = None


class SubstituteRespond(BaseModel):
    action: str  # "accept" | "reject"
    remark: Optional[str] = None

    @field_validator("action")
    @classmethod
    def validate_action(cls, v):
        if v not in ("accept", "reject"):
            raise ValueError("action 必須為 accept 或 reject")
        return v


# ============ Helpers ============


def _get_teacher_classroom_ids(session, emp_id: int) -> list[int]:
    """取得教師所屬班級 ID 列表"""
    classrooms = (
        session.query(Classroom)
        .filter(
            Classroom.is_active == True,
            or_(
                Classroom.head_teacher_id == emp_id,
                Classroom.assistant_teacher_id == emp_id,
                Classroom.art_teacher_id == emp_id,
            ),
        )
        .all()
    )
    return [c.id for c in classrooms]


def _get_teacher_student_ids(
    session,
    emp_id: int,
    classroom_id: int | None = None,
    forbidden_detail: str = "無權查看此班級的學生記錄",
) -> tuple[list[int], list[int]]:
    """取得教師有權查看的班級及學生 ID 列表。

    Returns:
        (classroom_ids, student_ids)：student_ids 為空代表無可查看學生，
        呼叫端應直接回傳空結果。

    Raises:
        HTTPException 403：指定 classroom_id 不屬於教師管轄班級。
    """
    classroom_ids = _get_teacher_classroom_ids(session, emp_id)
    if not classroom_ids:
        return classroom_ids, []

    if classroom_id:
        if classroom_id not in classroom_ids:
            raise HTTPException(status_code=403, detail=forbidden_detail)
        target_ids = [classroom_id]
    else:
        target_ids = classroom_ids

    student_ids = [
        s.id
        for s in session.query(Student.id)
        .filter(
            Student.classroom_id.in_(target_ids),
            Student.is_active == True,
        )
        .all()
    ]
    return target_ids, student_ids


def _get_employee(session, current_user: dict) -> Employee:
    employee_id = current_user.get("employee_id")
    if not employee_id:
        raise HTTPException(
            status_code=403,
            detail="此帳號無關聯員工資料，請先使用身份切換功能進入前台",
        )
    emp = session.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="找不到對應的員工資料")
    return emp


def _get_employee_shift_for_date(session, employee_id: int, target_date: date):
    """取得員工在指定日期的班別（優先 DailyShift -> ShiftAssignment）"""
    # 1. DailyShift override
    ds = (
        session.query(DailyShift)
        .filter(
            DailyShift.employee_id == employee_id,
            DailyShift.date == target_date,
        )
        .first()
    )
    if ds:
        return ds.shift_type_id

    # 2. Weekly ShiftAssignment
    week_monday = target_date - timedelta(days=target_date.weekday())
    sa = (
        session.query(ShiftAssignment)
        .filter(
            ShiftAssignment.employee_id == employee_id,
            ShiftAssignment.week_start_date == week_monday,
        )
        .first()
    )
    if sa:
        return sa.shift_type_id

    return None


def _get_shift_type_map(session, active_only: bool = False) -> dict:
    """取得 ShiftType {id: SimpleNamespace} 對照表（快取 5 分鐘）"""
    cache_key = "active" if active_only else "all"
    cached = _shift_type_cache.get(cache_key)
    if cached is not None:
        return cached
    from models.database import ShiftType

    query = session.query(ShiftType)
    if active_only:
        query = query.filter(ShiftType.is_active == True)
    result = {
        st.id: SimpleNamespace(
            id=st.id,
            name=st.name,
            work_start=st.work_start,
            work_end=st.work_end,
            sort_order=st.sort_order,
            is_active=st.is_active,
        )
        for st in query.all()
    }
    _shift_type_cache[cache_key] = result
    return result


# ============ ETag / Last-Modified helpers ============

import hashlib
from email.utils import format_datetime, parsedate_to_datetime

from fastapi import Request, Response as FastAPIResponse


def check_last_modified(request: Request, last_modified) -> FastAPIResponse | None:
    """If-Modified-Since 命中回 304；否則回 None（caller 繼續處理）。"""
    if last_modified is None:
        return None
    if_modified = request.headers.get("if-modified-since")
    if if_modified:
        try:
            client_dt = parsedate_to_datetime(if_modified)
            # floor 至秒（HTTP date 精度為秒）
            server_dt = last_modified.replace(microsecond=0)
            from datetime import timezone

            if client_dt.tzinfo is None:
                client_dt = client_dt.replace(tzinfo=timezone.utc)
            if server_dt.tzinfo is None:
                server_dt = server_dt.replace(tzinfo=timezone.utc)
            if server_dt <= client_dt:
                return FastAPIResponse(status_code=304)
        except (ValueError, TypeError):
            pass
    return None


def add_last_modified_header(response: FastAPIResponse, last_modified) -> None:
    response.headers["Last-Modified"] = format_datetime(last_modified, usegmt=True)


def check_etag(request: Request, etag: str) -> FastAPIResponse | None:
    """If-None-Match 命中回 304。"""
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match.strip('"').lstrip("W/").strip('"') == etag.strip(
        '"'
    ).lstrip("W/").strip('"'):
        return FastAPIResponse(status_code=304)
    return None


def compute_etag(payload) -> str:
    """`W/"<md5 16-char prefix>"` weak ETag。"""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    digest = hashlib.md5(payload).hexdigest()[:16]
    return f'W/"{digest}"'


def _calculate_annual_leave_quota(hire_date: date) -> int:
    """
    根據勞基法計算特休天數 (週年制)
    """
    if not hire_date:
        return 0

    today = date.today()
    months_diff = (today.year - hire_date.year) * 12 + today.month - hire_date.month
    if today.day < hire_date.day:
        months_diff -= 1

    years = months_diff // 12

    if months_diff < 6:
        return 0
    elif 6 <= months_diff < 12:
        return 3
    elif 1 <= years < 2:
        return 7
    elif 2 <= years < 3:
        return 10
    elif 3 <= years < 5:
        return 14
    elif 5 <= years < 10:
        return 15
    else:
        extra_days = years - 10
        total = 15 + extra_days
        return min(total, 30)
