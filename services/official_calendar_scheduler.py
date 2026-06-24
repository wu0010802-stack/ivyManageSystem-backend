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
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import get_settings, settings
from models.base import session_scope
from services.official_calendar import ensure_official_calendar_synced
from utils.advisory_lock import try_scheduler_lock
from utils.scheduler_observability import record_rows, scheduler_iteration

logger = logging.getLogger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.official_calendar_sync_enabled)


def _years_to_sync(now: datetime | None = None) -> list[int]:
    now = now or datetime.now(TAIPEI_TZ)
    years = [now.year]
    # 11~12 月開始抓下一年；政府通常於前一年下半年公布。
    if now.month >= 11:
        years.append(now.year + 1)
    return years


def sync_official_calendar_once(today: date | None = None) -> dict[int, str]:
    """對當年（必要時加下一年）執行一次強制同步，回傳 {year: status} 結果。

    多 worker 部署時以 advisory lock 確保同一日只有一個 worker 真的對上游發 request。
    取不到鎖（其他 worker 正在同步）回傳 ``{}``，外層 loop 不會 log（自然 falsy）。
    """
    today = today or datetime.now(TAIPEI_TZ).date()
    results: dict[int, str] = {}
    with session_scope() as lock_session:
        with try_scheduler_lock(
            lock_session,
            scheduler_name="official_calendar",
            run_key=today.isoformat(),
        ) as acquired:
            if not acquired:
                return {}
            for year in _years_to_sync():
                try:
                    with session_scope() as session:
                        info = ensure_official_calendar_synced(
                            session, year, force=True
                        )
                        results[year] = info.get("status", "unknown")
                except Exception:
                    # Downgraded：每年同步失敗不直觸 Sentry；scheduler iteration
                    # 整體失敗才由 wrapper 達閾值上報，避免上游 outage 期間每分鐘噴一筆
                    logger.warning("官方日曆排程同步失敗：year=%s", year, exc_info=True)
                    results[year] = "error"
    return results


async def run_official_calendar_scheduler(stop_event: asyncio.Event) -> None:
    # 最低 60 秒避免 hammering（與原邏輯一致）
    interval = max(settings.scheduler.official_calendar_sync_interval, 60)
    logger.info(
        "official calendar scheduler started (interval=%ds, tz=Asia/Taipei)",
        interval,
    )
    while not stop_event.is_set():
        with scheduler_iteration(
            "official_calendar", expected_interval_seconds=interval
        ):
            results = await asyncio.to_thread(sync_official_calendar_once)
            # 任一年 status=error 視為本次 iteration 失敗；wrapper 用 raise 觸發 throttle
            errored_years = [y for y, st in results.items() if st == "error"]
            record_rows("official_calendar", len(results) - len(errored_years))
            if errored_years:
                raise RuntimeError(
                    f"official calendar sync error years={errored_years}"
                )
            if results:
                logger.info("official calendar scheduler tick: %s", results)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
