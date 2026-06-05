"""scripts/seed/appraisal_yearend.py — 教師考核 + 年終獎金示範資料補齊。

目的：把 dev DB 既有的「考核 cycle」與「年終 cycle」補成全員一輪，
讓 grid / 簽核畫面有完整可視的一輪可看。

設計重點（對齊 task 約束）：
- **沿用既有 cycle**：不新增 appraisal_cycle / year_end_cycle，只補缺的子資料。
- **考核（INSERT 為主）**：為 19 位在職員工補 appraisal_participants + appraisal_summaries，
  summary 數值用引擎純函式 services.appraisal.engine.compute_* 直接算（不改 cycle 的 enrollment 欄位），
  另補幾筆 appraisal_score_items 讓明細頁有東西。既有的 emp 59/60 殼一律不動。
- **年終（UPDATE 為主）**：19 筆 year_end_settlements 已存在但全為 0；
  `(cycle_id, employee_id)` 唯一 → 無法 INSERT，只能 UPDATE 把金額填上。
  每筆自其 linked snapshot（snapshot_id → base_salary/festival_total/hire_months）餵
  services.year_end.engine.compute_settlement，special_bonus_total=0（total=payable）。
- **金額守衛**：所有年終金額遠在 ±100 萬 CHECK 內（薪資級距，數千元）。
- **簽核分佈**：直接設 status + *_signed_at/by（admin user）做出一輪 DRAFT/已簽/核定的分佈，
  保留數筆 DRAFT 供 live demo 實際簽核；兩 cycle 維持 OPEN（簽核端點要求 OPEN）。

【冪等契約】每筆插入前 exists 查；年終 UPDATE 以 calc_meta 內 marker 判定是否已 seed。
重跑：新增 0 筆、修改 0 筆。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalScoreItem,
    AppraisalSummary,
    AppraisalBonusRate,
    Grade,
    RoleGroup,
    SummaryStatus,
)
from models.year_end import (
    EmployeeYearEndSnapshot,
    YearEndCycle,
    YearEndSettlement,
    YearEndSettlementStatus,
)
from services.appraisal.employee_inference import (
    infer_classroom_id,
    infer_role_group,
)
from services.appraisal.engine import (
    BonusRateLookup,
    compute_summary,
)
from services.year_end.engine import (
    DeductionBreakdown,
    PerformanceRates,
    compute_settlement,
)

from scripts.seed._common import (
    get_active_employees,
    get_admin_user,
    session_scope,
)

logger = logging.getLogger(__name__)

# year_end_settlements.calc_meta 內的冪等 marker；重跑見到此鍵即跳過（0 修改）。
_SEED_MARKER = "seed:appraisal_yearend"

# 示範用：考核基礎分數的招生數（base = actual/target × 100 = 87.5）。
# 直接餵引擎純函式，不改 cycle 的 enrollment 欄位。
_DEMO_ENROLLMENT_ACTUAL = 140
_DEMO_ENROLLMENT_TARGET = 160

# 示範用：年終機構達成比率（Excel「年終獎金」sheet 的達成比率，如 83.6）。
_DEMO_ORG_ACHIEVEMENT_RATE = Decimal("83.6")
# 示範用：年終 step1 各率（全校達成率上/下 + 班級舊生/經營績效上/下）。
_DEMO_SCHOOL_RATE_FIRST = Decimal("87.5")
_DEMO_SCHOOL_RATE_SECOND = Decimal("91.5")
_DEMO_CLASS_RETURN_FIRST = Decimal("92.6")
_DEMO_CLASS_RETURN_SECOND = Decimal("94.9")
_DEMO_CLASS_PERF_FIRST = Decimal("94.4")
_DEMO_CLASS_PERF_SECOND = Decimal("88.5")

# 示範用 role_group 輪替：所有在職員工 infer 後皆為 STAFF；為讓 grid 視覺更貼近
# 真實一輪（多角色群、多等第），用 index 做確定性輪替（每群在 bonus_rates 都有對應率）。
_DEMO_ROLE_ROTATION: tuple[RoleGroup, ...] = (
    RoleGroup.SUPERVISOR,
    RoleGroup.HEAD_TEACHER,
    RoleGroup.HEAD_TEACHER,
    RoleGroup.ASSISTANT,
    RoleGroup.STAFF,
    RoleGroup.COOK,
)

# 示範用 event_score_sum 輪替（疊在 base 87.5 上 → 變動等第：優/甲/乙/丙）。
#   +5  → 92.5 OUTSTANDING（優，有獎金）
#   +0  → 87.5 GOOD       （甲，有獎金）
#   -10 → 77.5 PASS       （乙，無獎金）
#   -25 → 62.5 WARN       （丙，無獎金）
_DEMO_EVENT_DELTAS: tuple[Decimal, ...] = (
    Decimal("5"),
    Decimal("0"),
    Decimal("-10"),
    Decimal("-25"),
)

# 示範用簽核狀態輪替（保留數筆 DRAFT 給 live demo 實際簽核）。
_DEMO_SUMMARY_STATUS_ROTATION: tuple[SummaryStatus, ...] = (
    SummaryStatus.FINALIZED,
    SummaryStatus.ACCOUNTING_SIGNED,
    SummaryStatus.SUPERVISOR_SIGNED,
    SummaryStatus.DRAFT,
    SummaryStatus.DRAFT,
)
_DEMO_SETTLEMENT_STATUS_ROTATION: tuple[YearEndSettlementStatus, ...] = (
    YearEndSettlementStatus.FINALIZED,
    YearEndSettlementStatus.ACCOUNTING_SIGNED,
    YearEndSettlementStatus.SUPERVISOR_SIGNED,
    YearEndSettlementStatus.DRAFT,
    YearEndSettlementStatus.DRAFT,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _apply_summary_signoff(
    summary: AppraisalSummary, status: SummaryStatus, admin_id: int | None
) -> None:
    """依目標 status 設好對應的簽核時間/人欄位（直接設，不走 API 守衛）。"""
    summary.status = status
    now = _now()
    if status in (
        SummaryStatus.SUPERVISOR_SIGNED,
        SummaryStatus.ACCOUNTING_SIGNED,
        SummaryStatus.FINALIZED,
    ):
        summary.supervisor_signed_at = now
        summary.supervisor_signed_by = admin_id
    if status in (SummaryStatus.ACCOUNTING_SIGNED, SummaryStatus.FINALIZED):
        summary.accounting_signed_at = now
        summary.accounting_signed_by = admin_id
    if status == SummaryStatus.FINALIZED:
        summary.finalized_at = now
        summary.finalized_by = admin_id


def _apply_settlement_signoff(
    settlement: YearEndSettlement,
    status: YearEndSettlementStatus,
    admin_id: int | None,
) -> None:
    settlement.status = status
    now = _now()
    if status in (
        YearEndSettlementStatus.SUPERVISOR_SIGNED,
        YearEndSettlementStatus.ACCOUNTING_SIGNED,
        YearEndSettlementStatus.FINALIZED,
    ):
        settlement.supervisor_signed_at = now
        settlement.supervisor_signed_by = admin_id
    if status in (
        YearEndSettlementStatus.ACCOUNTING_SIGNED,
        YearEndSettlementStatus.FINALIZED,
    ):
        settlement.accounting_signed_at = now
        settlement.accounting_signed_by = admin_id
    if status == YearEndSettlementStatus.FINALIZED:
        settlement.finalized_at = now
        settlement.finalized_by = admin_id


def _seed_appraisal(session, admin_id: int | None) -> dict[str, int]:
    """補考核：participants + summaries + score_items（沿用既有 cycle）。"""
    counts = {"participants": 0, "summaries": 0, "score_items": 0}

    cycle = (
        session.execute(select(AppraisalCycle).order_by(AppraisalCycle.id).limit(1))
        .scalars()
        .first()
    )
    if cycle is None:
        logger.warning("無 appraisal_cycle，跳過考核 seed")
        return counts

    # 獎金率查表（引擎 compute_bonus_amount 用）。
    rate_rows = session.execute(select(AppraisalBonusRate)).scalars().all()
    bonus_lookup = BonusRateLookup(
        rates={
            (r.effective_from.isoformat(), r.role_group, r.grade): r.base_amount
            for r in rate_rows
        }
    )
    on_date = cycle.base_score_calc_date

    employees = get_active_employees(session)
    for idx, emp in enumerate(employees):
        role_group = _DEMO_ROLE_ROTATION[idx % len(_DEMO_ROLE_ROTATION)]
        # SUPERVISOR / HEAD_TEACHER / ASSISTANT 視同有班級績效 → 給個示範班級。
        classroom_id = infer_classroom_id(emp)
        if classroom_id is None and role_group in (
            RoleGroup.SUPERVISOR,
            RoleGroup.HEAD_TEACHER,
            RoleGroup.ASSISTANT,
        ):
            classroom_id = (idx % 11) + 1  # classrooms 1..11 皆存在

        # --- participant（exists 查 (cycle_id, employee_id)）---
        participant = (
            session.execute(
                select(AppraisalParticipant).filter_by(
                    cycle_id=cycle.id, employee_id=emp.id
                )
            )
            .scalars()
            .first()
        )
        if participant is None:
            participant = AppraisalParticipant(
                cycle_id=cycle.id,
                employee_id=emp.id,
                role_group=role_group,
                classroom_id=classroom_id,
                hire_months_in_cycle=Decimal("6"),
                is_excluded=False,
            )
            session.add(participant)
            session.flush()  # 取得 participant.id 供 summary / score_items FK
            counts["participants"] += 1

        # --- score_items（exists 查 (participant_id, item_code, sequence_no)）---
        # 補兩筆示範明細（auto 類加分 / 獎懲），讓考核明細頁有資料。
        event_delta = _DEMO_EVENT_DELTAS[idx % len(_DEMO_EVENT_DELTAS)]
        demo_items = [
            ("AFTER_CLASS_RATE", 1, Decimal("3.00"), "示範：才藝班參加率加分"),
            ("REWARD_PUNISH", 1, (event_delta - Decimal("3.00")), "示範：獎懲調整"),
        ]
        for item_code, seq, score_delta, note in demo_items:
            existing_item = (
                session.execute(
                    select(AppraisalScoreItem).filter_by(
                        participant_id=participant.id,
                        item_code=item_code,
                        sequence_no=seq,
                    )
                )
                .scalars()
                .first()
            )
            if existing_item is None:
                session.add(
                    AppraisalScoreItem(
                        participant_id=participant.id,
                        cycle_id=cycle.id,
                        catalog_id=None,
                        item_code=item_code,
                        sequence_no=seq,
                        score_delta=score_delta,
                        note=note,
                        created_by=admin_id,
                    )
                )
                counts["score_items"] += 1

        # --- summary（exists 查 participant_id，unique）---
        summary = (
            session.execute(
                select(AppraisalSummary).filter_by(participant_id=participant.id)
            )
            .scalars()
            .first()
        )
        if summary is None:
            # 用引擎純函式直接算（不改 cycle enrollment 欄位）。
            computed = compute_summary(
                actual_enrollment=_DEMO_ENROLLMENT_ACTUAL,
                enrollment_target=_DEMO_ENROLLMENT_TARGET,
                score_deltas=[d for _, _, d, _ in demo_items],
                role_group=role_group,
                bonus_rates=bonus_lookup,
                on_date=on_date,
            )
            summary = AppraisalSummary(
                participant_id=participant.id,
                cycle_id=cycle.id,
                base_score=computed.base_score,
                event_score_sum=computed.event_score_sum,
                total_score=computed.total_score,
                grade=computed.grade,
                bonus_amount=computed.bonus_amount,
            )
            status = _DEMO_SUMMARY_STATUS_ROTATION[
                idx % len(_DEMO_SUMMARY_STATUS_ROTATION)
            ]
            _apply_summary_signoff(summary, status, admin_id)
            session.add(summary)
            counts["summaries"] += 1

    return counts


def _seed_year_end(session, admin_id: int | None) -> dict[str, int]:
    """補年終：UPDATE 既有 19 筆 settlement 把金額填上（自 linked snapshot 餵引擎）。"""
    counts = {"settlements_updated": 0}

    cycle = (
        session.execute(select(YearEndCycle).order_by(YearEndCycle.id).limit(1))
        .scalars()
        .first()
    )
    if cycle is None:
        logger.warning("無 year_end_cycle，跳過年終 seed")
        return counts

    settlements = (
        session.execute(
            select(YearEndSettlement)
            .filter_by(year_end_cycle_id=cycle.id)
            .order_by(YearEndSettlement.employee_id)
        )
        .scalars()
        .all()
    )

    for idx, s in enumerate(settlements):
        # 冪等：calc_meta marker 已存在 → 跳過（0 修改）。
        if isinstance(s.calc_meta, dict) and s.calc_meta.get(_SEED_MARKER):
            continue

        snapshot = session.get(EmployeeYearEndSnapshot, s.snapshot_id)
        if snapshot is None:
            logger.warning("settlement id=%s 無 snapshot，跳過", s.id)
            continue

        base_salary = Decimal(snapshot.base_salary or 0)
        # snapshot.festival_total 目前為 0；給個示範節慶總額讓毛額更貼近真實。
        festival_total = (
            Decimal(snapshot.festival_total)
            if snapshot.festival_total
            else Decimal("2000")
        )
        hire_months = Decimal(snapshot.hire_months or 12)

        # 扣項示範：部分人有自強活動/機構會議未到（-1000）。
        deductions = DeductionBreakdown(
            meeting=(Decimal("-1000") if idx % 4 == 0 else Decimal("0")),
        )

        computed = compute_settlement(
            base_salary=base_salary,
            festival_total=festival_total,
            performance_rates=PerformanceRates(
                school_rate_first=_DEMO_SCHOOL_RATE_FIRST,
                school_rate_second=_DEMO_SCHOOL_RATE_SECOND,
                class_returning_rate_first=_DEMO_CLASS_RETURN_FIRST,
                class_returning_rate_second=_DEMO_CLASS_RETURN_SECOND,
                class_performance_rate_first=_DEMO_CLASS_PERF_FIRST,
                class_performance_rate_second=_DEMO_CLASS_PERF_SECOND,
            ),
            org_achievement_rate=_DEMO_ORG_ACHIEVEMENT_RATE,
            deductions=deductions,
            hire_months=hire_months,
            special_bonus_total=Decimal("0"),
        )

        # step1 各率（grid / 明細顯示用）。
        s.school_rate_first = _DEMO_SCHOOL_RATE_FIRST
        s.school_rate_second = _DEMO_SCHOOL_RATE_SECOND
        s.class_returning_rate_first = _DEMO_CLASS_RETURN_FIRST
        s.class_returning_rate_second = _DEMO_CLASS_RETURN_SECOND
        s.class_performance_rate_first = _DEMO_CLASS_PERF_FIRST
        s.class_performance_rate_second = _DEMO_CLASS_PERF_SECOND
        s.avg_performance_rate = computed.avg_performance_rate

        # step2-3
        s.base_salary = base_salary
        s.festival_total = festival_total
        s.gross_amount = computed.gross_amount
        s.org_achievement_rate = _DEMO_ORG_ACHIEVEMENT_RATE
        s.subtotal_amount = computed.subtotal_amount

        # step4（僅 meeting；其餘維持 0）
        s.deduction_meeting = deductions.meeting
        s.deduction_total = computed.deduction_total

        # step5-6
        s.hire_months = hire_months
        s.proration_rate = computed.proration_rate
        s.payable_amount = computed.payable_amount
        s.special_bonus_total = computed.special_bonus_total
        s.total_amount = computed.total_amount

        # calc_meta 加 marker（冪等鍵）。
        meta = dict(s.calc_meta) if isinstance(s.calc_meta, dict) else {}
        meta[_SEED_MARKER] = True
        s.calc_meta = meta
        flag_modified(s, "calc_meta")

        status = _DEMO_SETTLEMENT_STATUS_ROTATION[
            idx % len(_DEMO_SETTLEMENT_STATUS_ROTATION)
        ]
        _apply_settlement_signoff(s, status, admin_id)

        counts["settlements_updated"] += 1

    return counts


def step() -> None:
    """補齊考核 + 年終一輪示範資料（冪等）。"""
    with session_scope() as session:
        admin = get_admin_user(session)
        admin_id = admin.id if admin else None

        appraisal_counts = _seed_appraisal(session, admin_id)
        year_end_counts = _seed_year_end(session, admin_id)

    total = {**appraisal_counts, **year_end_counts}
    logger.info("appraisal_yearend seed 完成：%s", total)
    print(f"[seed.appraisal_yearend] {total}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    step()
