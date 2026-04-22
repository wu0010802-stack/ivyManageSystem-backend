"""薪資月底快照排程。

Lazy + 排程雙保險中的「排程」部分。
每日巡檢一次；若上個月有 SalaryRecord 但尚缺 month_end 快照，就補建。

- 單 worker 啟用 (`SALARY_AUTO_SNAPSHOT_ENABLED=1`)；即使多 worker 同啟，
  `create_month_end_snapshots` 本身 idempotent，最多 log 重複一次。
- 不等到特定時刻觸發；每小時巡檢，補缺就跑，下次巡檢自然 skip。
- 與 `api/salary.py` 的 lazy trigger 互補：即使整月沒人開薪資頁，排程仍會補。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from models.base import session_scope
from models.salary import SalaryRecord, SalarySnapshot
from services.salary_snapshot_service import create_month_end_snapshots

logger = logging.getLogger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

CHECK_INTERVAL_SECONDS = int(os.getenv("SALARY_SNAPSHOT_CHECK_INTERVAL", "3600"))


def scheduler_enabled() -> bool:
    return os.getenv("SALARY_AUTO_SNAPSHOT_ENABLED", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _today_taipei() -> date:
    return datetime.now(TAIPEI_TZ).date()


def _previous_month(today: date) -> tuple[int, int]:
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def check_and_snapshot_once(today: Optional[date] = None) -> int:
    """對「上個月」執行 idempotent 快照。回傳新建筆數。"""
    today = today or _today_taipei()
    year, month = _previous_month(today)
    with session_scope() as session:
        record_count = (
            session.query(SalaryRecord.id)
            .filter(
                SalaryRecord.salary_year == year,
                SalaryRecord.salary_month == month,
            )
            .count()
        )
        if record_count == 0:
            return 0
        snapshot_count = (
            session.query(SalarySnapshot.id)
            .filter(
                SalarySnapshot.salary_year == year,
                SalarySnapshot.salary_month == month,
                SalarySnapshot.snapshot_type == "month_end",
            )
            .count()
        )
        if snapshot_count >= record_count:
            return 0
        created = create_month_end_snapshots(session, year, month, "scheduler")
    return created


async def run_salary_snapshot_scheduler(stop_event: asyncio.Event) -> None:
    """每 CHECK_INTERVAL_SECONDS 巡檢一次；缺就補。"""
    logger.info(
        "salary snapshot scheduler started (interval=%ds, tz=Asia/Taipei)",
        CHECK_INTERVAL_SECONDS,
    )
    while not stop_event.is_set():
        try:
            created = check_and_snapshot_once()
            if created:
                logger.info("salary snapshot scheduler: created %d rows", created)
        except Exception:
            logger.exception("salary snapshot scheduler tick failed; continuing")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
