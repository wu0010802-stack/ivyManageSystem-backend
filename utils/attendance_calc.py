"""考勤狀態重算 helper。

依新的 punch_in / punch_out 與員工上下班時間，重算 Attendance 的派生欄位
（is_late / is_early_leave / is_missing_punch_* / late_minutes /
early_leave_minutes / status）。

抽出原因：補打卡核准（api/punch_corrections.py:approve）原本只改 punch_in_time
/ punch_out_time 與 missing 旗標，未重算 is_late / late_minutes 等；薪資 engine
直接讀這些 boolean / int 欄位（services/salary/engine.py:2099, 2114；
services/salary_field_breakdown.py:83, 95），導致補卡通過但仍扣遲到金。

Refs: 邏輯漏洞 audit 2026-05-07 P0 (#6)。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional, TypedDict

DEFAULT_WORK_START = "08:00"
DEFAULT_WORK_END = "17:00"


class AttendanceStatusFields(TypedDict):
    is_late: bool
    is_early_leave: bool
    is_missing_punch_in: bool
    is_missing_punch_out: bool
    late_minutes: int
    early_leave_minutes: int
    status: str


def _parse_work_time(value: Optional[str], default: str) -> time:
    return datetime.strptime(value or default, "%H:%M").time()


def recompute_attendance_status(
    *,
    attendance_date: date,
    punch_in_time: Optional[datetime],
    punch_out_time: Optional[datetime],
    work_start_str: Optional[str],
    work_end_str: Optional[str],
) -> AttendanceStatusFields:
    """依 punch 時間與員工排班時間重算考勤派生欄位。

    跨夜班（punch_out_time < punch_in_time + days）的修正由 caller 端負責，
    helper 接收已 normalize 的 datetime。
    """
    work_start = _parse_work_time(work_start_str, DEFAULT_WORK_START)
    work_end = _parse_work_time(work_end_str, DEFAULT_WORK_END)

    is_late = False
    is_early_leave = False
    is_missing_punch_in = punch_in_time is None
    is_missing_punch_out = punch_out_time is None
    late_minutes = 0
    early_leave_minutes = 0
    status = "normal"

    if punch_in_time:
        work_start_dt = datetime.combine(attendance_date, work_start)
        if punch_in_time > work_start_dt:
            is_late = True
            late_minutes = int((punch_in_time - work_start_dt).total_seconds() / 60)
            status = "late"

    if punch_out_time:
        work_end_dt = datetime.combine(attendance_date, work_end)
        if punch_out_time < work_end_dt:
            is_early_leave = True
            early_leave_minutes = int(
                (work_end_dt - punch_out_time).total_seconds() / 60
            )
            status = "early_leave" if status == "normal" else status + "+early_leave"

    if is_missing_punch_in:
        status = "missing" if status == "normal" else status + "+missing_in"
    if is_missing_punch_out:
        status = "missing" if status == "normal" else status + "+missing_out"

    return {
        "is_late": is_late,
        "is_early_leave": is_early_leave,
        "is_missing_punch_in": is_missing_punch_in,
        "is_missing_punch_out": is_missing_punch_out,
        "late_minutes": late_minutes,
        "early_leave_minutes": early_leave_minutes,
        "status": status,
    }


def apply_attendance_status(
    attendance,
    *,
    work_start_str: Optional[str],
    work_end_str: Optional[str],
) -> AttendanceStatusFields:
    """讀取 attendance.punch_in_time / punch_out_time / attendance_date 重算並寫回。

    供已有 ORM 物件的呼叫端使用（如 punch_corrections approve）。
    """
    fields = recompute_attendance_status(
        attendance_date=attendance.attendance_date,
        punch_in_time=attendance.punch_in_time,
        punch_out_time=attendance.punch_out_time,
        work_start_str=work_start_str,
        work_end_str=work_end_str,
    )
    attendance.is_late = fields["is_late"]
    attendance.is_early_leave = fields["is_early_leave"]
    attendance.is_missing_punch_in = fields["is_missing_punch_in"]
    attendance.is_missing_punch_out = fields["is_missing_punch_out"]
    attendance.late_minutes = fields["late_minutes"]
    attendance.early_leave_minutes = fields["early_leave_minutes"]
    attendance.status = fields["status"]
    return fields
