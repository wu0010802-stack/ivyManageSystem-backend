"""官方國定假日 / 補班日同步與後台行事曆 feed。"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from typing import Any

import requests
import urllib3
from requests import Response
from requests.exceptions import RequestException, SSLError
from sqlalchemy import or_

from models.database import Holiday, OfficialCalendarSync, SchoolEvent, WorkdayOverride

logger = logging.getLogger(__name__)

OFFICIAL_CALENDAR_DATASET_URL = "https://data.gov.tw/api/v2/rest/dataset/14718"
OFFICIAL_SOURCE = "dgpa"

EVENT_TYPE_LABELS = {
    "meeting": "會議",
    "activity": "活動",
    "holiday": "假日",
    "general": "一般",
    "makeup_workday": "補班日",
}


def _request_with_optional_ssl_fallback(url: str) -> Response:
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp
    except SSLError:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = requests.get(url, timeout=30, verify=False)
        resp.raise_for_status()
        return resp


def _get_resource_metadata(year: int) -> dict[str, str]:
    minguo_year = year - 1911
    resp = requests.get(OFFICIAL_CALENDAR_DATASET_URL, timeout=20)
    resp.raise_for_status()
    distribution = resp.json()["result"]["distribution"]
    prefix = f"{minguo_year}年"
    for item in distribution:
        desc = str(item.get("resourceDescription") or "")
        if desc.startswith(prefix):
            return {
                "download_url": item["resourceDownloadUrl"],
                "modified_at": item.get("resourceQualityCheckTime") or "",
                "description": desc,
            }
    raise ValueError(f"找不到 {year} 年官方辦公日曆資料")


def _parse_official_calendar_csv(csv_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    holidays: list[dict[str, Any]] = []
    makeup_days: list[dict[str, Any]] = []

    for row in reader:
        current_date = datetime.strptime(row["西元日期"], "%Y%m%d").date()
        note = (row.get("備註") or "").strip()
        is_weekend = current_date.weekday() >= 5
        holiday_flag = (row.get("是否放假") or "").strip()

        is_makeup = holiday_flag == "0" and is_weekend and ("補" in note and "上班" in note)
        if is_makeup:
            makeup_days.append({
                "date": current_date,
                "name": "補班日",
                "description": note or "補行上班",
            })
            continue

        is_named_or_weekday_holiday = holiday_flag == "2" and (bool(note) or not is_weekend)
        if is_named_or_weekday_holiday:
            holidays.append({
                "date": current_date,
                "name": note or "國定假日",
                "description": note or None,
            })

    return holidays, makeup_days


def _fetch_official_calendar_entries(year: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    resource = _get_resource_metadata(year)
    response = _request_with_optional_ssl_fallback(resource["download_url"])
    csv_text = response.content.decode("utf-8-sig", errors="replace")
    holidays, makeup_days = _parse_official_calendar_csv(csv_text)
    return holidays, makeup_days, resource


def _upsert_official_holidays(session, year: int, holidays: list[dict[str, Any]], synced_at: datetime) -> None:
    target_dates = [item["date"] for item in holidays]
    existing = {
        item.date: item
        for item in session.query(Holiday).filter(
            or_(
                (Holiday.source == OFFICIAL_SOURCE) & (Holiday.source_year == year),
                Holiday.date.in_(target_dates) if target_dates else False,
            ),
        ).all()
    }
    active_dates = set()
    for item in holidays:
        active_dates.add(item["date"])
        record = existing.get(item["date"])
        if record:
            record.name = item["name"]
            record.description = item["description"]
            record.is_active = True
            record.source = OFFICIAL_SOURCE
            record.source_year = year
            record.synced_at = synced_at
        else:
            session.add(Holiday(
                date=item["date"],
                name=item["name"],
                description=item["description"],
                is_active=True,
                source=OFFICIAL_SOURCE,
                source_year=year,
                synced_at=synced_at,
            ))

    for target_date, record in existing.items():
        if target_date not in active_dates:
            record.is_active = False
            record.synced_at = synced_at


def _upsert_official_makeup_days(session, year: int, makeup_days: list[dict[str, Any]], synced_at: datetime) -> None:
    target_dates = [item["date"] for item in makeup_days]
    existing = {
        item.date: item
        for item in session.query(WorkdayOverride).filter(
            or_(
                (WorkdayOverride.source == OFFICIAL_SOURCE) & (WorkdayOverride.source_year == year),
                WorkdayOverride.date.in_(target_dates) if target_dates else False,
            ),
        ).all()
    }
    active_dates = set()
    for item in makeup_days:
        active_dates.add(item["date"])
        record = existing.get(item["date"])
        if record:
            record.name = item["name"]
            record.description = item["description"]
            record.is_active = True
            record.source = OFFICIAL_SOURCE
            record.source_year = year
            record.synced_at = synced_at
        else:
            session.add(WorkdayOverride(
                date=item["date"],
                name=item["name"],
                description=item["description"],
                is_active=True,
                source=OFFICIAL_SOURCE,
                source_year=year,
                synced_at=synced_at,
            ))

    for target_date, record in existing.items():
        if target_date not in active_dates:
            record.is_active = False
            record.synced_at = synced_at


def _has_official_cache(session, year: int) -> bool:
    return (
        session.query(Holiday).filter(Holiday.source_year == year).count() > 0
        or session.query(WorkdayOverride).filter(WorkdayOverride.source_year == year).count() > 0
    )


def ensure_official_calendar_synced(session, year: int) -> dict[str, Any]:
    sync = session.query(OfficialCalendarSync).filter(
        OfficialCalendarSync.sync_year == year,
        OfficialCalendarSync.source == OFFICIAL_SOURCE,
    ).first()
    if not sync:
        sync = OfficialCalendarSync(sync_year=year, source=OFFICIAL_SOURCE)
        session.add(sync)
        session.flush()

    cache_available = _has_official_cache(session, year)

    try:
        resource = _get_resource_metadata(year)
    except Exception as exc:
        message = f"官方日曆同步失敗，{year} 年目前使用本地快取" if cache_available else f"官方日曆同步失敗：{exc}"
        sync.used_cache = cache_available
        sync.last_error = str(exc)
        session.commit()
        return {
            "status": "cached" if cache_available else "warning",
            "warning": message,
            "used_cache": cache_available,
            "last_synced_at": sync.last_synced_at.isoformat() if sync.last_synced_at else None,
        }

    is_stale = (
        not sync.is_synced
        or sync.source_modified_at != resource["modified_at"]
    )
    if not is_stale:
        return {
            "status": "synced",
            "warning": None,
            "used_cache": False,
            "last_synced_at": sync.last_synced_at.isoformat() if sync.last_synced_at else None,
        }

    try:
        holidays, makeup_days, resource_meta = _fetch_official_calendar_entries(year)
        synced_at = datetime.now()
        _upsert_official_holidays(session, year, holidays, synced_at)
        _upsert_official_makeup_days(session, year, makeup_days, synced_at)
        sync.is_synced = True
        sync.used_cache = False
        sync.last_synced_at = synced_at
        sync.last_error = None
        sync.source_modified_at = resource_meta["modified_at"]
        session.commit()
        return {
            "status": "synced",
            "warning": None,
            "used_cache": False,
            "last_synced_at": synced_at.isoformat(),
        }
    except Exception as exc:
        session.rollback()
        sync = session.query(OfficialCalendarSync).filter(
            OfficialCalendarSync.sync_year == year,
            OfficialCalendarSync.source == OFFICIAL_SOURCE,
        ).first()
        if sync:
            sync.used_cache = cache_available
            sync.last_error = str(exc)
            session.commit()
        logger.warning("官方日曆同步失敗：year=%s error=%s", year, exc)
        return {
            "status": "cached" if cache_available else "warning",
            "warning": (
                f"官方日曆同步失敗，{year} 年目前使用本地快取"
                if cache_available
                else f"官方日曆同步失敗：{exc}"
            ),
            "used_cache": cache_available,
            "last_synced_at": sync.last_synced_at.isoformat() if sync and sync.last_synced_at else None,
        }


def _school_event_to_feed_dict(event: SchoolEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "title": event.title,
        "description": event.description,
        "event_date": event.event_date.isoformat(),
        "end_date": event.end_date.isoformat() if event.end_date else None,
        "event_type": event.event_type,
        "event_type_label": EVENT_TYPE_LABELS.get(event.event_type, event.event_type),
        "is_all_day": event.is_all_day,
        "start_time": event.start_time,
        "end_time": event.end_time,
        "location": event.location,
        "is_official": False,
        "is_read_only": False,
        "official_kind": None,
    }


def _official_item_to_feed_dict(item, official_kind: str, event_type: str) -> dict[str, Any]:
    return {
        "id": f"{official_kind}-{item.id}",
        "title": item.name,
        "description": item.description,
        "event_date": item.date.isoformat(),
        "end_date": None,
        "event_type": event_type,
        "event_type_label": EVENT_TYPE_LABELS.get(event_type, event_type),
        "is_all_day": True,
        "start_time": None,
        "end_time": None,
        "location": None,
        "is_official": True,
        "is_read_only": True,
        "official_kind": official_kind,
    }


def build_admin_calendar_feed(session, year: int, month: int) -> dict[str, Any]:
    sync_info = ensure_official_calendar_synced(session, year)

    import calendar as cal_module
    _, last_day = cal_module.monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)

    manual_events = session.query(SchoolEvent).filter(
        SchoolEvent.is_active.is_(True),
        SchoolEvent.event_date <= end,
        or_(
            SchoolEvent.end_date >= start,
            (SchoolEvent.end_date.is_(None)) & (SchoolEvent.event_date >= start),
        ),
    ).all()
    holidays = session.query(Holiday).filter(
        Holiday.is_active.is_(True),
        Holiday.date >= start,
        Holiday.date <= end,
    ).all()
    makeup_days = session.query(WorkdayOverride).filter(
        WorkdayOverride.is_active.is_(True),
        WorkdayOverride.date >= start,
        WorkdayOverride.date <= end,
    ).all()

    events = (
        [_school_event_to_feed_dict(item) for item in manual_events]
        + [_official_item_to_feed_dict(item, "holiday", "holiday") for item in holidays]
        + [_official_item_to_feed_dict(item, "makeup_workday", "makeup_workday") for item in makeup_days]
    )
    events.sort(key=lambda item: (item["event_date"], item["title"]))

    return {
        "year": year,
        "month": month,
        "events": events,
        "official_sync": sync_info,
    }
