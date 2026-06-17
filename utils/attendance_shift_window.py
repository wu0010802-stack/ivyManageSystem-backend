"""(employee, date) → 班別視窗 datetime 的唯一解析來源。
優先序：DailyShift > 週排班(僅導師/助教) > 員工自訂 work_start/end_time > 08:00/17:00。
end 落在 start 之前/相同視為跨夜，+1 日。三條匯入路徑與預覽端點共用，確保「預覽所見 = 匯入所得」。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from utils.attendance_calc import compute_shift_aware_status

_DEFAULT_START = "08:00"
_DEFAULT_END = "17:00"


def resolve_shift_window(
    employee,
    attendance_date,
    daily_shift_map: dict,
    shift_schedule_map: dict,
    *,
    is_head_teacher: bool = False,
    is_assistant: bool = False,
) -> tuple[datetime, datetime]:
    ws = we = None
    daily = daily_shift_map.get((employee.id, attendance_date))
    if daily and daily.get("work_start") and daily.get("work_end"):
        ws, we = daily["work_start"], daily["work_end"]
    elif is_head_teacher or is_assistant:
        week_start = attendance_date - timedelta(days=attendance_date.weekday())
        wa = shift_schedule_map.get((employee.id, week_start))
        if wa and wa.get("work_start") and wa.get("work_end"):
            ws, we = wa["work_start"], wa["work_end"]
    if ws is None or we is None:
        ws = getattr(employee, "work_start_time", None) or _DEFAULT_START
        we = getattr(employee, "work_end_time", None) or _DEFAULT_END
    start_dt = datetime.combine(attendance_date, datetime.strptime(ws, "%H:%M").time())
    end_dt = datetime.combine(attendance_date, datetime.strptime(we, "%H:%M").time())
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def compute_status_for_employee_date(
    employee,
    attendance_date,
    punch_in_dt: Optional[datetime],
    punch_out_dt: Optional[datetime],
    daily_shift_map: dict,
    shift_schedule_map: dict,
    *,
    is_head_teacher: bool = False,
    is_assistant: bool = False,
) -> tuple[bool, int, bool, int, str]:
    start_dt, end_dt = resolve_shift_window(
        employee,
        attendance_date,
        daily_shift_map,
        shift_schedule_map,
        is_head_teacher=is_head_teacher,
        is_assistant=is_assistant,
    )
    return compute_shift_aware_status(punch_in_dt, punch_out_dt, start_dt, end_dt)
