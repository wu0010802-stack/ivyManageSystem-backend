"""Summary 副作用：stale 標記、active rates 載入。

放在獨立模組避免 engine.py 與 catalog.py 互相 import。
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalBonusRate,
    AppraisalParticipant,
    AppraisalSummary,
    Grade,
    RoleGroup,
    SummaryStatus,
)

logger = logging.getLogger(__name__)


def load_active_rates_map(
    db: Session, on_date: Optional[date] = None
) -> dict[tuple[RoleGroup, Grade], Decimal]:
    """取 effective_from <= on_date 中最新一版 rate，組成 lookup map。"""
    on_date = on_date or date.today()
    rows = (
        db.execute(
            select(AppraisalBonusRate)
            .where(AppraisalBonusRate.effective_from <= on_date)
            .order_by(AppraisalBonusRate.effective_from.desc())
        )
        .scalars()
        .all()
    )
    seen: dict[tuple[RoleGroup, Grade], Decimal] = {}
    for r in rows:
        key = (r.role_group, r.grade)
        seen.setdefault(key, r.base_amount)
    return seen


def mark_summary_stale(
    db: Session,
    participant_id: int,
    *,
    reset_signatures: bool = True,
) -> Optional[AppraisalSummary]:
    """Score item 變更時呼叫：重算 summary + status reset 到 DRAFT。

    FINALIZED → raise PermissionError；caller 應 patch 時擋下並回 409。
    """
    from .engine import recompute_summary

    summary = db.execute(
        select(AppraisalSummary).where(
            AppraisalSummary.participant_id == participant_id
        )
    ).scalar_one_or_none()
    if summary is None:
        return None
    if summary.status == SummaryStatus.FINALIZED:
        raise PermissionError(
            "summary_finalized:cannot_modify_underlying_score_items"
        )
    participant = db.get(AppraisalParticipant, participant_id)
    recompute_summary(db, summary, participant)
    if reset_signatures and summary.status != SummaryStatus.DRAFT:
        summary.status = SummaryStatus.DRAFT
        summary.supervisor_signed_at = None
        summary.supervisor_signed_by = None
        summary.supervisor_comment = None
        summary.accounting_signed_at = None
        summary.accounting_signed_by = None
        summary.accounting_comment = None
    return summary
