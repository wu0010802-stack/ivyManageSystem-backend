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
    ShiftAssignment, ShiftType, DailyShift, Holiday, AttendancePolicy,
)
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

        # 系統預設上下班時間（當員工無排班時使用）
        policy = session.query(AttendancePolicy).first()
        default_ws = policy.default_work_start if policy and policy.default_work_start else "08:00"
        default_we = policy.default_work_end if policy and policy.default_work_end else "17:00"

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
                    "source": source,
                })

            cur += timedelta(days=1)

        return {"total_hours": round(total_hours * 2) / 2, "breakdown": breakdown}
    finally:
        session.close()
