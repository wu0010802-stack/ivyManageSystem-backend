"""
Workday hours calculation router
"""

import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import joinedload

from models.database import (
    get_session,
    ShiftAssignment, ShiftType, DailyShift, AttendancePolicy,
)
from services.workday_rules import classify_day, load_day_rule_maps
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

workday_router = APIRouter()


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


def _normalize_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value[:5]


def _to_minutes(value: str) -> int:
    hh, mm = map(int, value.split(":"))
    return hh * 60 + mm


def _calc_bounded_shift_hours(work_start: str, work_end: str, start_bound: Optional[str], end_bound: Optional[str]) -> float:
    day_start = max(work_start, _normalize_time(start_bound) or work_start)
    day_end = min(work_end, _normalize_time(end_bound) or work_end)
    if day_start >= day_end:
        return 0.0

    minutes = _to_minutes(day_end) - _to_minutes(day_start)
    overlap_start = max(_to_minutes(day_start), _to_minutes("12:00"))
    overlap_end = min(_to_minutes(day_end), _to_minutes("13:00"))
    if overlap_end > overlap_start:
        minutes -= (overlap_end - overlap_start)
    return max(0.0, round((minutes / 60) * 2) / 2)


def _build_workday_hours_payload(session, employee_id: int, start_date: date, end_date: date) -> dict:
    holiday_map, makeup_map = load_day_rule_maps(session, start_date, end_date)

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

    policy = session.query(AttendancePolicy).first()
    default_ws = policy.default_work_start if policy and policy.default_work_start else "08:00"
    default_we = policy.default_work_end if policy and policy.default_work_end else "17:00"

    breakdown = []
    total_hours = 0.0
    cur = start_date

    while cur <= end_date:
        weekday = cur.weekday()
        day_rule = classify_day(cur, holiday_map, makeup_map)

        if day_rule["kind"] == "weekend":
            breakdown.append({
                "date": cur.isoformat(),
                "weekday": weekday,
                "type": "weekend",
                "hours": 0,
                "shift": None,
                "work_start": None,
                "work_end": None,
                "holiday_name": None,
                "is_makeup_workday": False,
                "workday_override_name": None,
                "source": None,
            })
        elif day_rule["kind"] == "holiday":
            breakdown.append({
                "date": cur.isoformat(),
                "weekday": weekday,
                "type": "holiday",
                "hours": 0,
                "shift": None,
                "work_start": None,
                "work_end": None,
                "holiday_name": day_rule["holiday_name"],
                "is_makeup_workday": False,
                "workday_override_name": None,
                "source": None,
            })
        else:
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
                hours = _calc_shift_hours(default_ws, default_we)
                shift_name = None
                work_start = default_ws
                work_end = default_we

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
                "is_makeup_workday": day_rule["is_makeup_workday"],
                "workday_override_name": day_rule["workday_override_name"],
                "source": source,
            })

        cur += timedelta(days=1)

    return {"total_hours": round(total_hours * 2) / 2, "breakdown": breakdown}


def calculate_leave_work_hours(
    session,
    employee_id: int,
    start_date: date,
    end_date: date,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> float:
    payload = _build_workday_hours_payload(session, employee_id, start_date, end_date)
    start_bound = _normalize_time(start_time)
    end_bound = _normalize_time(end_time)
    if not start_bound and not end_bound:
        return payload["total_hours"]

    total_hours = 0.0
    start_key = start_date.isoformat()
    end_key = end_date.isoformat()
    for day in payload["breakdown"]:
        if day["type"] != "workday":
            continue
        day_start_bound = start_bound if day["date"] == start_key else None
        day_end_bound = end_bound if day["date"] == end_key else None
        total_hours += _calc_bounded_shift_hours(
            day["work_start"],
            day["work_end"],
            day_start_bound,
            day_end_bound,
        )
    return round(total_hours * 2) / 2


def validate_leave_hours_against_schedule(
    session,
    employee_id: int,
    start_date: date,
    end_date: date,
    leave_hours: float,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> None:
    max_hours = calculate_leave_work_hours(
        session,
        employee_id,
        start_date,
        end_date,
        start_time,
        end_time,
    )
    if leave_hours > max_hours:
        if max_hours <= 0:
            raise HTTPException(
                status_code=400,
                detail="所選區間沒有可請假的工作時數，已自動排除週末與國定假日",
            )
        raise HTTPException(
            status_code=400,
            detail=(
                f"請假時數 {leave_hours}h 超過該區間可請假的工作時數 {max_hours}h，"
                "系統已自動排除週末與國定假日"
            ),
        )


@workday_router.get("/leaves/workday-hours")
def get_workday_hours(
    employee_id: int,
    start_date: date,
    end_date: date,
    current_user: dict = Depends(require_staff_permission(Permission.LEAVES_READ)),
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
        return _build_workday_hours_payload(session, employee_id, start_date, end_date)
    finally:
        session.close()
