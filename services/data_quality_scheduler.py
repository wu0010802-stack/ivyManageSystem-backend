"""services/data_quality_scheduler.py — 每日 03:00 跑 data quality rules。

沿用 finance_reconciliation_scheduler.py 的 pattern：asyncio loop + opt-in env
+ try_scheduler_lock 防多 worker 重複跑 + scheduler_iteration observability。
"""

import asyncio
import logging
from datetime import date, datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

from config import get_settings
from models.database import get_session
from services.data_quality.dispatch import emit, flush_line_digest
from services.data_quality.engine import run_all_rules
from utils.scheduler_observability import record_rows, scheduler_iteration

logger = logging.getLogger(__name__)
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 每日 03:00 跑一次 → heartbeat expected interval 為一天。
# 注意不可用 check_interval（60s 巡檢週期），否則 /health/schedulers
# 以 lag > 2×expected 判定時，日級 job 永遠被誤判 lagging。
_DAILY_INTERVAL_SEC = 24 * 60 * 60


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.data_quality_enabled)


def _target_hm() -> tuple[int, int]:
    s = get_settings().scheduler
    return (s.data_quality_hour, s.data_quality_minute)


def should_run_data_quality(
    now: datetime,
    target_hour: int,
    target_minute: int,
    last_run_date: Optional[date] = None,
) -> bool:
    """到/過每日目標時刻且當日尚未跑 → 觸發（純函式，便於單元測試）。

    原本 now.minute == target_minute 精準分鐘比對 + 巡檢，相位漂移後輪詢落在
    target 分鐘之外即整天錯過；改為 >= 目標時刻 + 當日去重，沿用
    finance_reconciliation_scheduler.should_run_reconciliation 的修法。
    """
    if last_run_date == now.date():
        return False
    return now.time() >= time(target_hour, target_minute)


def run_data_quality_once() -> dict:
    """同步執行一輪 rule + dispatch；可手動觸發。

    Returns:
        dict with keys: detected (int), new_open (int), ran_at (ISO str).
    """
    line_queue: list = []
    session = get_session()
    try:
        violations = run_all_rules(session)
        new_open = 0
        for v in violations:
            if emit(v, session, line_queue=line_queue):
                new_open += 1
        flush_line_digest(line_queue)
        return {
            "detected": len(violations),
            "new_open": new_open,
            "ran_at": datetime.now(TAIPEI_TZ).isoformat(),
        }
    finally:
        session.close()


async def run_data_quality_scheduler(stop_event: asyncio.Event) -> None:
    """每分鐘檢查當下時間是否到 target_hm；到了拿 advisory lock 跑一次。

    沿用 finance_reconciliation_scheduler.py 的 wait_for(stop_event) sleep idiom，
    以便 shutdown 時快速中斷。
    """
    from utils.advisory_lock import try_scheduler_lock

    target_hour, target_minute = _target_hm()
    check_interval = get_settings().scheduler.data_quality_check_interval
    logger.info(
        "資料品質排程啟動（每日 %02d:%02d Asia/Taipei，巡檢週期 %ss）",
        target_hour,
        target_minute,
        check_interval,
    )

    last_run_date: Optional[object] = None
    while not stop_event.is_set():
        if not scheduler_enabled():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
            except asyncio.TimeoutError:
                pass
            continue

        try:
            now = datetime.now(TAIPEI_TZ)
            if should_run_data_quality(now, target_hour, target_minute, last_run_date):
                session = get_session()
                try:
                    with try_scheduler_lock(
                        session,
                        scheduler_name="data_quality",
                        run_key=now.date().isoformat(),
                    ) as acquired:
                        if acquired:
                            with scheduler_iteration(
                                "data_quality",
                                expected_interval_seconds=_DAILY_INTERVAL_SEC,
                            ):
                                result = await asyncio.to_thread(run_data_quality_once)
                                record_rows(
                                    "data_quality",
                                    int(result.get("detected", 0) or 0),
                                )
                                # C35：fail-closed。last_run_date / 成功 log 必須在
                                # scheduler_iteration 區塊 *內*——若放區塊外，
                                # run_data_quality_once 拋例外被 swallow 後仍會把當日
                                # 標成已跑完（致整天不重試）且引用未綁定的 result。
                                # 對齊 graduation_scheduler 的 fail-closed 寫法。
                                last_run_date = now.date()
                                logger.info(
                                    "資料品質排程完成 date=%s result=%s",
                                    now.date().isoformat(),
                                    result,
                                )
                        else:
                            logger.info(
                                "資料品質排程：已有其他 worker 在執行 date=%s，本次略過",
                                now.date().isoformat(),
                            )
                finally:
                    session.close()
        except Exception:
            logger.exception("資料品質排程巡檢失敗（忽略本次）")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass
