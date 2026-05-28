"""services/leave_quota_expiry_scheduler.py — 每日輪詢補休到期 + 特休週年 cutover。

沿用 services/recruitment_term_advance_scheduler.py /
services/graduation_scheduler.py asyncio polling pattern。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import get_settings
from utils.scheduler_observability import scheduler_iteration

logger = logging.getLogger(__name__)


def _today_taipei() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.leave_quota_expiry_enabled)


async def run_leave_quota_expiry_scheduler(stop_event: asyncio.Event) -> None:
    """每日輪詢補休到期 + 特休週年 cutover。

    - last_run_date 記憶體 guard 確保每日只跑一次（避免 log spam）
    - try_scheduler_lock advisory lock 防多 instance 重複跑（run_key=today.isoformat()）
    - session_scope context manager
    """
    from models.base import session_scope
    from utils.advisory_lock import try_scheduler_lock
    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants
    from services.leave_quota_expiry.annual_cutover import (
        cutover_annual_leave_anniversaries,
    )
    from services.leave_quota_expiry.comp_grant_reminder import (
        remind_upcoming_comp_grants,
    )

    check_interval = get_settings().scheduler.leave_quota_expiry_check_interval
    logger.info("leave quota expiry scheduler 啟動 (interval=%ss)", check_interval)

    last_run_date: date | None = None

    while not stop_event.is_set():
        with scheduler_iteration(
            "leave_quota_expiry",
            expected_interval_seconds=check_interval,
        ):
            today = _today_taipei()
            if last_run_date != today:
                with session_scope() as session:
                    with try_scheduler_lock(
                        session,
                        scheduler_name="leave_quota_expiry",
                        run_key=today.isoformat(),
                    ) as acquired:
                        if acquired:
                            comp_summary = expire_comp_leave_grants(today, session)
                            cutover_summary = cutover_annual_leave_anniversaries(
                                today, session
                            )
                            reminder_summary = remind_upcoming_comp_grants(
                                today, session
                            )
                            logger.info(
                                "leave quota expiry tick: %s | %s | %s",
                                comp_summary,
                                cutover_summary,
                                reminder_summary,
                            )
                            last_run_date = today
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass
