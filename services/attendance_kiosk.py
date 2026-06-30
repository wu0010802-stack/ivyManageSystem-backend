"""園內 kiosk 即時打卡核心邏輯（純函式，session 由 caller 傳入，便於單元測試）。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from models.database import Attendance


@dataclass
class PunchPreview:
    employee_name: str
    action: str  # "punch_in" | "punch_out"
    will_overwrite: bool
    current_punch_out: Optional[datetime]
    server_time: datetime


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
