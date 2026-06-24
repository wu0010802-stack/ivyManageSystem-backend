"""services/academic_term_turnover_scheduler.py — 學期自動切換驅動器。

唯一的學期切換來源（取代手動 set-current）。每個週期 reconcile：
- 用 _resolve_by_date(today) 算出當前學期 T
- 比對 is_current row C：缺→靜默 seed（無事件）；C≠T→翻牌 + fire_term_changed（含事件）；
  C==T→no-op（用 is_current 當標記，天然冪等）
首次部署誤觸發由 acadterm01 migration 靜默對齊防護。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import get_settings
from models.academic_term import AcademicTerm
from models.audit import AuditLog
from utils.academic import _resolve_by_date, term_bounds
from utils.taipei_time import now_taipei_naive
from utils.scheduler_observability import record_rows, scheduler_iteration
from utils.term_events import fire_term_changed

logger = logging.getLogger(__name__)


def _today_taipei() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.academic_term_turnover_enabled)


def _get_or_create_term(session, school_year: int, semester: int) -> AcademicTerm:
    row = (
        session.query(AcademicTerm)
        .filter(
            AcademicTerm.school_year == school_year,
            AcademicTerm.semester == semester,
        )
        .first()
    )
    if row is not None:
        return row
    start, end = term_bounds(school_year, semester)
    row = AcademicTerm(
        school_year=school_year,
        semester=semester,
        start_date=start,
        end_date=end,
        is_current=False,
    )
    session.add(row)
    session.flush()
    return row


def reconcile_academic_term(session, *, today: date) -> dict:
    """核心 reconcile（同步、在 caller 的 session/transaction 內）。

    回傳 {"action": "seed"|"turnover"|"noop", "term": "<sy>-<sem>"}。
    caller 負責 commit / rollback（handler raise 會 propagate）。
    """
    sy, sem = _resolve_by_date(today)
    current = (
        session.query(AcademicTerm).filter(AcademicTerm.is_current.is_(True)).first()
    )

    if current is None:
        # 缺：全新 DB / 從未種子化 → 靜默基準，不觸發事件
        target = _get_or_create_term(session, sy, sem)
        target.is_current = True
        session.flush()
        logger.info("academic_term seed（靜默）：%s-%s", sy, sem)
        return {"action": "seed", "term": f"{sy}-{sem}"}

    if current.school_year == sy and current.semester == sem:
        return {"action": "noop", "term": f"{sy}-{sem}"}

    # 真的跨界 → 翻牌 + 觸發結轉事件
    old = current
    # 先清舊 is_current 並 flush，再設新的；避免兩列同時 true 撞 partial unique singleton index
    old.is_current = False
    session.flush()
    target = _get_or_create_term(session, sy, sem)
    target.is_current = True
    session.flush()
    fire_term_changed(old=old, new=target, session=session)
    session.add(
        AuditLog(
            user_id=None,
            username="academic_term_turnover",
            action="UPDATE",
            entity_type="academic_term",
            entity_id=str(target.id),
            summary=(f"學期自動切換：{old.school_year}-{old.semester} → {sy}-{sem}"),
            changes=json.dumps(
                {
                    "from": f"{old.school_year}-{old.semester}",
                    "to": f"{sy}-{sem}",
                },
                ensure_ascii=False,
            ),
            ip_address=None,
            created_at=now_taipei_naive(),
        )
    )
    logger.info(
        "學期自動切換：%s-%s → %s-%s",
        old.school_year,
        old.semester,
        sy,
        sem,
    )
    return {"action": "turnover", "term": f"{sy}-{sem}"}


async def run_academic_term_turnover_scheduler(stop_event: asyncio.Event) -> None:
    """每日輪詢；loop 第一圈即時跑（涵蓋啟動時補抓停機期間跨界）。"""
    from models.base import session_scope

    check_interval = get_settings().scheduler.academic_term_turnover_check_interval
    logger.info("academic term turnover scheduler 啟動 (interval=%ss)", check_interval)
    while not stop_event.is_set():
        with scheduler_iteration(
            "academic_term_turnover", expected_interval_seconds=check_interval
        ):

            def _run_turnover():
                with session_scope() as session:
                    return reconcile_academic_term(session, today=_today_taipei())

            # 同步 DB 工作丟 threadpool，不在 event loop 上跑（與 security_gc 一致）。
            out = await asyncio.to_thread(_run_turnover)
            record_rows(
                "academic_term_turnover",
                1 if out["action"] == "turnover" else 0,
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass
