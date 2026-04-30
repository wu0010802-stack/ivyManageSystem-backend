"""官方日曆每日同步排程。

背景每日跑一次 ``ensure_official_calendar_synced(year, force=True)``，
- 同步當年（current year）。
- 11~12 月時順帶同步下一年，銜接政府提前公布的隔年辦公日曆。

頁面 feed 預設讀取本地快取（24h 內）；上游 TLS / timeout 故障不再拖慢使用者操作。

啟用方式：環境變數 ``OFFICIAL_CALENDAR_SYNC_ENABLED=1``；建議僅單一 worker 開。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from models.base import session_scope
from services.official_calendar import ensure_official_calendar_synced

logger = logging.getLogger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 預設一天一次；環境變數可調（單位：秒）。最低 60 秒避免 hammering。
CHECK_INTERVAL_SECONDS = max(
    int(os.getenv("OFFICIAL_CALENDAR_SYNC_INTERVAL", "86400")), 60
)


def scheduler_enabled() -> bool:
    return os.getenv("OFFICIAL_CALENDAR_SYNC_ENABLED", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _years_to_sync(now: datetime | None = None) -> list[int]:
    now = now or datetime.now(TAIPEI_TZ)
    years = [now.year]
    # 11~12 月開始抓下一年；政府通常於前一年下半年公布。
    if now.month >= 11:
        years.append(now.year + 1)
    return years


def sync_official_calendar_once() -> dict[int, str]:
    """對當年（必要時加下一年）執行一次強制同步，回傳 {year: status} 結果。"""
    results: dict[int, str] = {}
    for year in _years_to_sync():
        try:
            with session_scope() as session:
                info = ensure_official_calendar_synced(session, year, force=True)
                results[year] = info.get("status", "unknown")
        except Exception:
            logger.exception("官方日曆排程同步失敗：year=%s", year)
            results[year] = "error"
    return results


async def run_official_calendar_scheduler(stop_event: asyncio.Event) -> None:
    logger.info(
        "official calendar scheduler started (interval=%ds, tz=Asia/Taipei)",
        CHECK_INTERVAL_SECONDS,
    )
    while not stop_event.is_set():
        try:
            results = sync_official_calendar_once()
            if results:
                logger.info("official calendar scheduler tick: %s", results)
        except Exception:
            logger.exception("official calendar scheduler tick crashed; continuing")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
