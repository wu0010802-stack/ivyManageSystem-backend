"""services/recruitment_term_advance_scheduler.py — 每日心跳，
在 academic_terms.start_date 當天批量推進 enrolled → active。

照 services/graduation_scheduler.py 的結構。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import get_settings
from models.academic_term import AcademicTerm
from services.recruitment_lifecycle import advance_term_to_active

logger = logging.getLogger(__name__)


def _today_taipei() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.recruitment_term_advance_enabled)


async def run_recruitment_term_advance_scheduler(stop_event: asyncio.Event) -> None:
    """每日輪詢：今天 = 某 term.start_date 則推進該 term 的 enrolled 學生。

    照 graduation_scheduler 樣式，session_scope 寫入 → log 結果。
    """
    from models.base import session_scope  # 延遲匯入避免循環

    check_interval = get_settings().scheduler.recruitment_term_advance_check_interval
    logger.info(
        "recruitment term advance scheduler 啟動 (interval=%ss)", check_interval
    )

    while not stop_event.is_set():
        try:
            today = _today_taipei()
            with session_scope() as session:
                terms = (
                    session.query(AcademicTerm)
                    .filter(AcademicTerm.start_date == today)
                    .all()
                )
                for term in terms:
                    summary = advance_term_to_active(
                        session,
                        term.school_year,
                        term.semester,
                    )
                    logger.info(
                        "term advance year=%s sem=%s %s",
                        term.school_year,
                        term.semester,
                        summary,
                    )
        except Exception:
            logger.exception("term advance scheduler tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass
