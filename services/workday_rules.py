"""工作日 / 假日 / 補班日共用判定。"""

from __future__ import annotations

from datetime import date

from models.database import Holiday, WorkdayOverride


def load_day_rule_maps(session, start_date: date, end_date: date) -> tuple[dict[date, str], dict[date, str]]:
    holiday_map = {
        item.date: item.name
        for item in session.query(Holiday).filter(
            Holiday.date >= start_date,
            Holiday.date <= end_date,
            Holiday.is_active.is_(True),
        ).all()
    }
    makeup_map = {
        item.date: item.name
        for item in session.query(WorkdayOverride).filter(
            WorkdayOverride.date >= start_date,
            WorkdayOverride.date <= end_date,
            WorkdayOverride.is_active.is_(True),
        ).all()
    }
    return holiday_map, makeup_map


def classify_day(target_date: date, holiday_map: dict[date, str], makeup_map: dict[date, str]) -> dict:
    if target_date in makeup_map:
        return {
            "kind": "workday",
            "is_weekend": False,
            "is_holiday": False,
            "is_makeup_workday": True,
            "holiday_name": None,
            "workday_override_name": makeup_map[target_date],
        }

    holiday_name = holiday_map.get(target_date)
    if holiday_name:
        return {
            "kind": "holiday",
            "is_weekend": False,
            "is_holiday": True,
            "is_makeup_workday": False,
            "holiday_name": holiday_name,
            "workday_override_name": None,
        }

    is_weekend = target_date.weekday() >= 5
    return {
        "kind": "weekend" if is_weekend else "workday",
        "is_weekend": is_weekend,
        "is_holiday": False,
        "is_makeup_workday": False,
        "holiday_name": None,
        "workday_override_name": None,
    }

