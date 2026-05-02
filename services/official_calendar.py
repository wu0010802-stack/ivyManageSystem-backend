"""官方國定假日 / 補班日同步與後台行事曆 feed。"""

from __future__ import annotations

import csv
import io
import logging
import ssl
from datetime import date, datetime, timedelta
from typing import Any

import requests
from requests import Response
from requests.adapters import HTTPAdapter
from sqlalchemy import or_

from models.database import Holiday, OfficialCalendarSync, SchoolEvent, WorkdayOverride

logger = logging.getLogger(__name__)

OFFICIAL_CALENDAR_DATASET_URL = "https://data.gov.tw/api/v2/rest/dataset/14718"
OFFICIAL_SOURCE = "dgpa"
DGPA_HOST_PREFIX = "https://www.dgpa.gov.tw"

# 快取新鮮窗：頁面讀取若 last_synced_at 在此期間內即直接走快取，不打上游
# 排程每日 sync 一次，所以 24h 是預設安全值（排程未啟用時也不會比舊行為差）
_FRESH_CACHE_WINDOW = timedelta(hours=24)

EVENT_TYPE_LABELS = {
    "meeting": "會議",
    "activity": "活動",
    "holiday": "假日",
    "general": "一般",
    "makeup_workday": "補班日",
}

_FRIENDLY_SYNC_WARNING = "官方日曆暫時無法同步，目前顯示本地快取資料"
_FRIENDLY_SYNC_WARNING_NO_CACHE = "官方日曆暫時無法同步，請稍後再試或聯絡管理員"


class _DgpaSSLAdapter(HTTPAdapter):
    """針對 www.dgpa.gov.tw 的 TLS adapter。

    保留 CA 與 hostname 驗證，僅關閉 OpenSSL X509 strict flag，
    避開政府站常見的 Missing Subject Key Identifier 之類嚴格憑證錯誤。
    """

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(*args, **kwargs)


def _create_official_session() -> requests.Session:
    session = requests.Session()
    session.mount(DGPA_HOST_PREFIX, _DgpaSSLAdapter())
    return session


def _request_with_optional_ssl_fallback(url: str) -> Response:
    # 不啟用 verify=False；DGPA 主機走 _DgpaSSLAdapter 放寬 X509 strict flag，
    # 其他主機維持嚴格驗證。函式名保留是為了避免改動外部 import。
    with _create_official_session() as session:
        resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp


def _select_official_distribution(distribution: list[dict], minguo_year: int) -> dict:
    """從 data.gov.tw 回傳的 distribution 清單中挑出官方標準 CSV。

    篩選順序：
    1. 描述以 `{minguo_year}年` 開頭。
    2. 排除 Google 專用版本 / 非標準下載連結（Google Calendar URL 等）。
    3. 多筆候選時依 resourceQualityCheckTime 取最新。
    """
    prefix = f"{minguo_year}年"
    candidates: list[dict] = []
    for item in distribution:
        desc = str(item.get("resourceDescription") or "")
        url = str(item.get("resourceDownloadUrl") or "")
        if not desc.startswith(prefix):
            continue
        if not url:
            continue
        lowered_desc = desc.lower()
        lowered_url = url.lower()
        if "google" in lowered_desc or "google" in lowered_url:
            continue
        if "calendar.google" in lowered_url:
            continue
        candidates.append(item)
    if not candidates:
        raise ValueError(f"找不到 {minguo_year + 1911} 年官方辦公日曆資料")
    candidates.sort(
        key=lambda x: str(x.get("resourceQualityCheckTime") or ""),
        reverse=True,
    )
    return candidates[0]


def _get_resource_metadata(year: int) -> dict[str, str]:
    minguo_year = year - 1911
    resp = requests.get(OFFICIAL_CALENDAR_DATASET_URL, timeout=20)
    resp.raise_for_status()
    distribution = resp.json()["result"]["distribution"]
    item = _select_official_distribution(distribution, minguo_year)
    return {
        "download_url": item["resourceDownloadUrl"],
        "modified_at": item.get("resourceQualityCheckTime") or "",
        "description": str(item.get("resourceDescription") or ""),
    }


def _parse_official_calendar_csv(
    csv_text: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    holidays: list[dict[str, Any]] = []
    makeup_days: list[dict[str, Any]] = []

    for row in reader:
        current_date = datetime.strptime(row["西元日期"], "%Y%m%d").date()
        note = (row.get("備註") or "").strip()
        is_weekend = current_date.weekday() >= 5
        holiday_flag = (row.get("是否放假") or "").strip()

        is_makeup = (
            holiday_flag == "0" and is_weekend and ("補" in note and "上班" in note)
        )
        if is_makeup:
            makeup_days.append(
                {
                    "date": current_date,
                    "name": "補班日",
                    "description": note or "補行上班",
                }
            )
            continue

        is_named_or_weekday_holiday = holiday_flag == "2" and (
            bool(note) or not is_weekend
        )
        if is_named_or_weekday_holiday:
            holidays.append(
                {
                    "date": current_date,
                    "name": note or "國定假日",
                    "description": note or None,
                }
            )

    return holidays, makeup_days


def _fetch_official_calendar_entries(
    year: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    resource = _get_resource_metadata(year)
    response = _request_with_optional_ssl_fallback(resource["download_url"])
    csv_text = response.content.decode("utf-8-sig", errors="replace")
    holidays, makeup_days = _parse_official_calendar_csv(csv_text)
    return holidays, makeup_days, resource


def _upsert_official_holidays(
    session, year: int, holidays: list[dict[str, Any]], synced_at: datetime
) -> None:
    target_dates = [item["date"] for item in holidays]
    existing = {
        item.date: item
        for item in session.query(Holiday)
        .filter(
            or_(
                (Holiday.source == OFFICIAL_SOURCE) & (Holiday.source_year == year),
                Holiday.date.in_(target_dates) if target_dates else False,
            ),
        )
        .all()
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
            session.add(
                Holiday(
                    date=item["date"],
                    name=item["name"],
                    description=item["description"],
                    is_active=True,
                    source=OFFICIAL_SOURCE,
                    source_year=year,
                    synced_at=synced_at,
                )
            )

    for target_date, record in existing.items():
        if target_date not in active_dates:
            record.is_active = False
            record.synced_at = synced_at


def _upsert_official_makeup_days(
    session, year: int, makeup_days: list[dict[str, Any]], synced_at: datetime
) -> None:
    target_dates = [item["date"] for item in makeup_days]
    existing = {
        item.date: item
        for item in session.query(WorkdayOverride)
        .filter(
            or_(
                (WorkdayOverride.source == OFFICIAL_SOURCE)
                & (WorkdayOverride.source_year == year),
                WorkdayOverride.date.in_(target_dates) if target_dates else False,
            ),
        )
        .all()
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
            session.add(
                WorkdayOverride(
                    date=item["date"],
                    name=item["name"],
                    description=item["description"],
                    is_active=True,
                    source=OFFICIAL_SOURCE,
                    source_year=year,
                    synced_at=synced_at,
                )
            )

    for target_date, record in existing.items():
        if target_date not in active_dates:
            record.is_active = False
            record.synced_at = synced_at


def _has_official_cache(session, year: int) -> bool:
    return (
        session.query(Holiday).filter(Holiday.source_year == year).count() > 0
        or session.query(WorkdayOverride)
        .filter(WorkdayOverride.source_year == year)
        .count()
        > 0
    )


def _is_cache_fresh(sync: OfficialCalendarSync | None) -> bool:
    if not sync or not sync.last_synced_at or sync.last_error:
        return False
    return datetime.now() - sync.last_synced_at < _FRESH_CACHE_WINDOW


def get_cached_official_sync_status(session, year: int) -> dict[str, Any]:
    """純讀本地快取，不打上游。前景 feed 用，避免阻塞使用者操作。

    回傳 shape 與 ``ensure_official_calendar_synced`` 一致：
    - 快取新鮮：``status="synced"``
    - 快取存在但過期：``status="cached"`` + warning（提醒排程未跟上）
    - 完全無快取：``status="warning"`` + warning（提示需先 force sync）

    冷啟動 / 排程未啟用時這裡只會回 warning，使用者仍可看到（可能空的）feed，
    不會被 DGPA 網路或 TLS 故障拖住請求。實際對上游同步交給
    ``services/official_calendar_scheduler``（背景每日 force sync）或
    管理員手動觸發 ``ensure_official_calendar_synced(force=True)``。
    """
    sync = (
        session.query(OfficialCalendarSync)
        .filter(
            OfficialCalendarSync.sync_year == year,
            OfficialCalendarSync.source == OFFICIAL_SOURCE,
        )
        .first()
    )
    cache_available = _has_official_cache(session, year)
    last_synced_iso = (
        sync.last_synced_at.isoformat() if sync and sync.last_synced_at else None
    )

    if not cache_available:
        return {
            "status": "warning",
            "warning": _FRIENDLY_SYNC_WARNING_NO_CACHE,
            "used_cache": False,
            "last_synced_at": last_synced_iso,
        }

    if _is_cache_fresh(sync):
        return {
            "status": "synced",
            "warning": None,
            "used_cache": False,
            "last_synced_at": last_synced_iso,
        }

    # 快取存在但過期或上次同步留有 last_error：仍顯示舊資料 + 警告
    return {
        "status": "cached",
        "warning": _FRIENDLY_SYNC_WARNING,
        "used_cache": True,
        "last_synced_at": last_synced_iso,
    }


def ensure_official_calendar_synced(
    session, year: int, *, force: bool = False
) -> dict[str, Any]:
    """確保 ``year`` 年官方日曆已同步並回傳目前狀態。

    - ``force=False``（預設，頁面 feed 用）：若 24h 內已成功同步且本地快取存在，
      直接回快取狀態，不打 data.gov.tw 與 dgpa；冷啟動或快取過期才嘗試上游。
    - ``force=True``（背景排程或一次性指令用）：強制嘗試與上游比對版本並 upsert。
    """
    sync = (
        session.query(OfficialCalendarSync)
        .filter(
            OfficialCalendarSync.sync_year == year,
            OfficialCalendarSync.source == OFFICIAL_SOURCE,
        )
        .first()
    )
    if not sync:
        sync = OfficialCalendarSync(sync_year=year, source=OFFICIAL_SOURCE)
        session.add(sync)
        session.flush()

    cache_available = _has_official_cache(session, year)

    if not force and cache_available and _is_cache_fresh(sync):
        return {
            "status": "synced",
            "warning": None,
            "used_cache": False,
            "last_synced_at": sync.last_synced_at.isoformat(),
        }

    try:
        resource = _get_resource_metadata(year)
    except Exception as exc:
        message = (
            _FRIENDLY_SYNC_WARNING
            if cache_available
            else _FRIENDLY_SYNC_WARNING_NO_CACHE
        )
        sync.used_cache = cache_available
        sync.last_error = str(exc)
        session.commit()
        logger.exception("官方日曆 metadata 取得失敗：year=%s", year)
        return {
            "status": "cached" if cache_available else "warning",
            "warning": message,
            "used_cache": cache_available,
            "last_synced_at": (
                sync.last_synced_at.isoformat() if sync.last_synced_at else None
            ),
        }

    is_stale = not sync.is_synced or sync.source_modified_at != resource["modified_at"]
    if not is_stale:
        sync.last_error = None
        sync.used_cache = False
        sync.last_synced_at = datetime.now()
        session.commit()
        return {
            "status": "synced",
            "warning": None,
            "used_cache": False,
            "last_synced_at": (
                sync.last_synced_at.isoformat() if sync.last_synced_at else None
            ),
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
        sync = (
            session.query(OfficialCalendarSync)
            .filter(
                OfficialCalendarSync.sync_year == year,
                OfficialCalendarSync.source == OFFICIAL_SOURCE,
            )
            .first()
        )
        if sync:
            sync.used_cache = cache_available
            sync.last_error = str(exc)
            session.commit()
        logger.exception("官方日曆同步失敗：year=%s", year)
        return {
            "status": "cached" if cache_available else "warning",
            "warning": (
                _FRIENDLY_SYNC_WARNING
                if cache_available
                else _FRIENDLY_SYNC_WARNING_NO_CACHE
            ),
            "used_cache": cache_available,
            "last_synced_at": (
                sync.last_synced_at.isoformat()
                if sync and sync.last_synced_at
                else None
            ),
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


def _official_item_to_feed_dict(
    item, official_kind: str, event_type: str
) -> dict[str, Any]:
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
    # 前景只讀本地快取，不打上游；DGPA TLS / timeout 故障不拖慢使用者操作。
    # 上游 freshness 由 official_calendar_scheduler 背景排程維護。
    sync_info = get_cached_official_sync_status(session, year)

    import calendar as cal_module

    _, last_day = cal_module.monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)

    manual_events = (
        session.query(SchoolEvent)
        .filter(
            SchoolEvent.is_active.is_(True),
            SchoolEvent.event_date <= end,
            or_(
                SchoolEvent.end_date >= start,
                (SchoolEvent.end_date.is_(None)) & (SchoolEvent.event_date >= start),
            ),
        )
        .all()
    )
    holidays = (
        session.query(Holiday)
        .filter(
            Holiday.is_active.is_(True),
            Holiday.date >= start,
            Holiday.date <= end,
        )
        .all()
    )
    makeup_days = (
        session.query(WorkdayOverride)
        .filter(
            WorkdayOverride.is_active.is_(True),
            WorkdayOverride.date >= start,
            WorkdayOverride.date <= end,
        )
        .all()
    )

    events = (
        [_school_event_to_feed_dict(item) for item in manual_events]
        + [_official_item_to_feed_dict(item, "holiday", "holiday") for item in holidays]
        + [
            _official_item_to_feed_dict(item, "makeup_workday", "makeup_workday")
            for item in makeup_days
        ]
    )
    events.sort(key=lambda item: (item["event_date"], item["title"]))

    return {
        "year": year,
        "month": month,
        "events": events,
        "official_sync": sync_info,
    }
