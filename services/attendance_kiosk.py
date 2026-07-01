"""園內 kiosk 即時打卡核心邏輯（純函式，session 由 caller 傳入，便於單元測試）。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from models.database import Attendance
from utils.attendance_leave_merge import (
    merge_attendance_with_leave,
    reset_confirmation_if_changed,
    snapshot_attendance_confirmation_inputs,
)
from utils.attendance_shift_window import (
    build_shift_maps_for_employee_date,
    compute_status_for_employee_date,
)
from utils.approval_helpers import _get_finalized_salary_record


class MonthFinalizedError(Exception):
    """該員工該月薪資已封存，拒絕寫入考勤。"""


@dataclass
class PunchPreview:
    employee_name: str
    action: str  # "punch_in" | "punch_out"
    will_overwrite: bool
    current_punch_out: Optional[datetime]
    server_time: datetime


@dataclass
class PunchResult:
    employee_name: str
    action: str
    punch_time: datetime
    status: str


def _today_row(session, employee, attendance_date):
    return (
        session.query(Attendance)
        .filter(
            Attendance.employee_id == employee.id,
            Attendance.attendance_date == attendance_date,
        )
        .first()
    )


def resolve_punch_action(session, employee, now_dt: datetime) -> PunchPreview:
    """依當天既有列判定本次打卡為上班/下班（first-in / last-out），純讀不寫。"""
    row = _today_row(session, employee, now_dt.date())
    if row is None or row.punch_in_time is None:
        action, will_overwrite, cur_out = "punch_in", False, None
    elif row.punch_out_time is None:
        action, will_overwrite, cur_out = "punch_out", False, None
    else:
        action, will_overwrite, cur_out = "punch_out", True, row.punch_out_time
    return PunchPreview(
        employee_name=employee.name,
        action=action,
        will_overwrite=will_overwrite,
        current_punch_out=cur_out,
        server_time=now_dt,
    )


def apply_punch(session, employee, now_dt: datetime) -> PunchResult:
    """寫入當天考勤列（first-in/last-out）、重算 status、同步請假、標薪資 stale、commit。"""
    from services.salary.utils import lock_and_premark_stale  # 延遲 import 避免循環

    attendance_date = now_dt.date()

    # 封存守衛
    if _get_finalized_salary_record(
        session, employee.id, attendance_date.year, attendance_date.month
    ):
        raise MonthFinalizedError(
            f"{attendance_date.year} 年 {attendance_date.month} 月薪資已封存，無法打卡。"
        )

    row = _today_row(session, employee, attendance_date)
    _row_existed = row is not None  # 追蹤是否為既有列（新建 row 無豁免可清）
    if row is None:
        row = Attendance(
            employee_id=employee.id, attendance_date=attendance_date, status="normal"
        )
        session.add(row)

    # 快照（必須在任何欄位覆寫之前；僅對既有 row，與 records.py F-D 不變量一致）
    if _row_existed:
        _confirm_before = snapshot_attendance_confirmation_inputs(row)

    # first-in / last-out
    if row.punch_in_time is None:
        row.punch_in_time = now_dt
        action = "punch_in"
    else:
        row.punch_out_time = now_dt
        action = "punch_out"

    # 跨夜防禦（kiosk 同列遞增通常不觸發；保留與 records.py 一致行為）
    if (
        row.punch_in_time
        and row.punch_out_time
        and row.punch_out_time < row.punch_in_time
    ):
        row.punch_out_time = row.punch_out_time + timedelta(days=1)

    # 重算 status
    daily_map, week_map = build_shift_maps_for_employee_date(
        session, employee, attendance_date
    )
    is_late, late_min, is_early, early_min, status = compute_status_for_employee_date(
        employee,
        attendance_date,
        row.punch_in_time,
        row.punch_out_time,
        daily_map,
        week_map,
        is_head_teacher=getattr(employee, "is_head_teacher", False),
        is_assistant=getattr(employee, "is_assistant", False),
    )
    row.status = status
    row.is_late = is_late
    row.late_minutes = late_min
    row.is_early_leave = is_early
    row.early_leave_minutes = early_min
    row.is_missing_punch_in = row.punch_in_time is None
    row.is_missing_punch_out = row.punch_out_time is None
    row.source = "kiosk"

    # 豁免重置（punch/旗標實質改變時清 confirmed_action，防 admin_waive 殘留致薪資漏扣；
    # 必須在 merge_attendance_with_leave 之前，與 records.py F-D ordering 一致）
    if _row_existed:
        reset_confirmation_if_changed(row, _confirm_before)

    # 請假同步
    merge_attendance_with_leave(row, session)

    # 標該月薪資需重算
    lock_and_premark_stale(
        session, employee.id, {(attendance_date.year, attendance_date.month)}
    )

    # commit 前先取值，避免 expire_on_commit 後再讀 row/employee 觸發 reload
    punch_time = row.punch_in_time if action == "punch_in" else row.punch_out_time
    status_val = row.status
    emp_name = employee.name
    session.commit()
    return PunchResult(
        employee_name=emp_name,
        action=action,
        punch_time=punch_time,
        status=status_val,
    )
