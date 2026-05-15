"""半年考核計算引擎（5 step）。

step 1：base_score 已由 router 寫到 participant（依 monthly_enrollment_snapshots 計算）
step 2：item_score_sum = Σ score_items.score_delta
step 3：total_score = clamp(base + sum, 0, 110)
step 4：grade = classify_grade(total)
step 5：bonus = base_amount(role, grade) × GRADE_BONUS_PCT[grade]

純函式無狀態；資料庫操作由 caller 注入 session。
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalParticipant,
    AppraisalScoreItem,
    AppraisalSummary,
    Grade,
    RoleGroup,
    SummaryStatus,
)

from .constants import (
    GRADE_BONUS_PCT,
    GRADE_CUT_GOOD,
    GRADE_CUT_OUTSTANDING,
    GRADE_CUT_PASS,
    GRADE_CUT_WARN,
    MAX_TOTAL_SCORE,
    MIN_TOTAL_SCORE,
)

logger = logging.getLogger(__name__)


def classify_grade(total_score: Decimal) -> Grade:
    """依總分回傳等第。優≥90 / 甲80-89 / 乙70-79 / 丙60-69 / 丁<60。"""
    if total_score >= GRADE_CUT_OUTSTANDING:
        return Grade.OUTSTANDING
    if total_score >= GRADE_CUT_GOOD:
        return Grade.GOOD
    if total_score >= GRADE_CUT_PASS:
        return Grade.PASS
    if total_score >= GRADE_CUT_WARN:
        return Grade.WARN
    return Grade.FAIL


def compute_total_score(base_score: Decimal, item_sum: Decimal) -> Decimal:
    raw = base_score + item_sum
    if raw < MIN_TOTAL_SCORE:
        return MIN_TOTAL_SCORE
    if raw > MAX_TOTAL_SCORE:
        return MAX_TOTAL_SCORE
    return raw.quantize(Decimal("0.01"))


def compute_bonus_amount(
    grade: Grade,
    role_group: RoleGroup,
    rates_map: dict[tuple[RoleGroup, Grade], Decimal],
) -> Decimal:
    """獎金 = base_amount × GRADE_BONUS_PCT[grade]。

    無對應 rate / 丙丁等回 0（不擋計算，避免設定漏建撐爆 router）。
    """
    pct = GRADE_BONUS_PCT.get(grade, Decimal("0"))
    if pct == 0:
        return Decimal("0")
    base = rates_map.get((role_group, grade))
    if base is None:
        return Decimal("0")
    return (base * pct).quantize(Decimal("0.01"))


def recompute_summary(
    db: Session,
    summary: AppraisalSummary,
    participant: AppraisalParticipant,
    rates_map: Optional[dict[tuple[RoleGroup, Grade], Decimal]] = None,
) -> AppraisalSummary:
    """重算 summary：5 step 計算後寫回 summary 欄位。

    呼叫端負責 commit。FINALIZED summary 不可呼叫（自檢 ValueError）。
    """
    if summary.status == SummaryStatus.FINALIZED:
        raise ValueError("Cannot recompute FINALIZED summary")

    if rates_map is None:
        from .summary_ops import load_active_rates_map

        rates_map = load_active_rates_map(db)

    deltas = (
        db.execute(
            select(AppraisalScoreItem.score_delta).where(
                AppraisalScoreItem.participant_id == participant.id
            )
        )
        .scalars()
        .all()
    )
    item_sum: Decimal = sum(deltas, Decimal("0"))

    summary.base_score = participant.base_score
    summary.item_score_sum = item_sum.quantize(Decimal("0.01"))
    summary.total_score = compute_total_score(summary.base_score, summary.item_score_sum)
    summary.grade = classify_grade(summary.total_score)
    summary.bonus_amount = compute_bonus_amount(
        summary.grade, participant.role_group, rates_map
    )
    summary.version += 1
    return summary
