"""週工時超時預警工具模組

提供兩層功能：
  1. 純計算層（無 DB 依賴，可直接單元測試）
  2. DB 查詢層（需要 SQLAlchemy session）
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

WEEKLY_WORK_HOURS_LIMIT = 40.0


# ---------------------------------------------------------------------------
# 純計算層
# ---------------------------------------------------------------------------

def calculate_shift_hours(work_start: str, work_end: str) -> float:
    """計算單一班別工時（小時）。

    Args:
        work_start: 上班時間，格式 HH:MM
        work_end:   下班時間，格式 HH:MM

    Returns:
        工時小時數（float）。跨夜班（end <= start）自動加 24 小時。
    """
    sh, sm = map(int, work_start.split(":"))
    eh, em = map(int, work_end.split(":"))
    start_minutes = sh * 60 + sm
    end_minutes = eh * 60 + em
    if end_minutes <= start_minutes:
        end_minutes += 1440  # 跨夜班
    return (end_minutes - start_minutes) / 60.0


def get_week_dates(target_date: date) -> List[date]:
    """回傳 target_date 所在週的 7 天（週一到週日）。"""
    monday = target_date - timedelta(days=target_date.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def compute_weekly_hours(shift_hours_per_date: Dict[date, Optional[float]]) -> float:
    """加總週工時；None 視為 0（排休或無班）。"""
    return sum(h for h in shift_hours_per_date.values() if h is not None)


def build_weekly_warning(
    employee_id: int,
    employee_name: str,
    week_start: date,
    weekly_hours: float,
    limit: float = WEEKLY_WORK_HOURS_LIMIT,
) -> Optional[dict]:
    """超過 limit 才回傳 warning dict，否則回傳 None。

    Warning dict 格式：
        code, employee_id, employee_name, week_start,
        calculated_hours, limit_hours, message
    """
    if weekly_hours <= limit:
        return None
    return {
        "code": "WEEKLY_HOURS_EXCEEDED",
        "employee_id": employee_id,
        "employee_name": employee_name,
        "week_start": week_start.isoformat(),
        "calculated_hours": round(weekly_hours, 2),
        "limit_hours": limit,
        "message": (
            f"{employee_name} 本週排班工時 {weekly_hours:.1f} 小時，"
            f"超過勞基法上限 {limit:.0f} 小時"
        ),
    }


# ---------------------------------------------------------------------------
# DB 查詢層
# ---------------------------------------------------------------------------

def get_employee_weekly_shift_hours(
    session,
    employee_id: int,
    week_dates: List[date],
    shift_type_map: dict,
    overrides: Optional[Dict[date, Optional[int]]] = None,
) -> Dict[date, Optional[float]]:
    """取得員工一整週每天的工時。

    優先級：overrides > DailyShift > ShiftAssignment

    Args:
        session:         SQLAlchemy session
        employee_id:     員工 ID
        week_dates:      週一到週日的 7 個 date 物件（由 get_week_dates 產生）
        shift_type_map:  {shift_type_id: ShiftType 物件} 對照表
        overrides:       {date: shift_type_id or None}
                         換班假設狀態；None 表示該日在換班後明確排休

    Returns:
        {date: float or None}，None 代表排休/無班
    """
    from models.database import DailyShift, ShiftAssignment

    week_start = week_dates[0]
    week_end = week_dates[-1]

    # 一次查詢整週 DailyShift（避免 N+1）
    daily_shifts = session.query(DailyShift).filter(
        DailyShift.employee_id == employee_id,
        DailyShift.date >= week_start,
        DailyShift.date <= week_end,
    ).all()
    daily_map: Dict[date, Optional[int]] = {ds.date: ds.shift_type_id for ds in daily_shifts}
    daily_override_dates = {ds.date for ds in daily_shifts}

    # 查週排班（ShiftAssignment）
    sa = session.query(ShiftAssignment).filter(
        ShiftAssignment.employee_id == employee_id,
        ShiftAssignment.week_start_date == week_start,
    ).first()
    weekly_shift_type_id: Optional[int] = sa.shift_type_id if sa else None

    overrides = overrides or {}
    result: Dict[date, Optional[float]] = {}

    for d in week_dates:
        if d in overrides:
            shift_id = overrides[d]           # overrides 優先（含 None 排休）
        elif d in daily_override_dates:
            shift_id = daily_map[d]           # DailyShift 其次
        else:
            shift_id = weekly_shift_type_id   # ShiftAssignment 最低

        if shift_id is None:
            result[d] = None
        else:
            st = shift_type_map.get(shift_id)
            result[d] = calculate_shift_hours(st.work_start, st.work_end) if st else None

    return result


def check_weekly_hours_warning(
    session,
    employee_id: int,
    employee_name: str,
    target_date: date,
    shift_type_map: dict,
    overrides: Optional[Dict[date, Optional[int]]] = None,
) -> Optional[dict]:
    """換班 / 儲存排班共用的高層入口。

    計算 target_date 所在週的預測工時；超過 40 小時則回傳 warning dict，否則回傳 None。
    """
    week_dates = get_week_dates(target_date)
    shift_hours = get_employee_weekly_shift_hours(
        session, employee_id, week_dates, shift_type_map, overrides
    )
    weekly_hours = compute_weekly_hours(shift_hours)
    return build_weekly_warning(employee_id, employee_name, week_dates[0], weekly_hours)
