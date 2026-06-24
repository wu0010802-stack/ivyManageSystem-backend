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
from utils.scheduler_observability import record_rows, scheduler_iteration

logger = logging.getLogger(__name__)


def _today_taipei() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


# Catch-up 視窗（天）：term.start_date 當天若停機，開機後 N 天內仍補推進。
# advance_term_to_active 只推 ENROLLED→active，對已 active 者 idempotent，重跑安全。
TERM_ADVANCE_CATCHUP_DAYS = 7


def term_start_date_window(today: date) -> tuple[date, date]:
    """要處理的 term.start_date 範圍 [today - grace, today]。

    原本精準 start_date == today，當天整段停機就永久錯過；改為有界區間補跑。
    """
    from datetime import timedelta

    return today - timedelta(days=TERM_ADVANCE_CATCHUP_DAYS), today


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
        with scheduler_iteration(
            "recruitment_term_advance", expected_interval_seconds=check_interval
        ):
            today = _today_taipei()

            def _run_term_advance():
                advanced = 0
                with session_scope() as session:
                    lo, hi = term_start_date_window(today)
                    terms = (
                        session.query(AcademicTerm)
                        .filter(
                            AcademicTerm.start_date >= lo,
                            AcademicTerm.start_date <= hi,
                        )
                        .all()
                    )
                    for term in terms:
                        summary = advance_term_to_active(
                            session,
                            term.school_year,
                            term.semester,
                        )
                        advanced += (
                            int(summary.get("advanced", 0) or 0)
                            if isinstance(summary, dict)
                            else 0
                        )
                        logger.info(
                            "term advance year=%s sem=%s %s",
                            term.school_year,
                            term.semester,
                            summary,
                        )
                return advanced

            # 同步 DB 工作丟 threadpool，不在 event loop 上跑（與 security_gc 一致）。
            advanced = await asyncio.to_thread(_run_term_advance)
            record_rows("recruitment_term_advance", advanced)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass
