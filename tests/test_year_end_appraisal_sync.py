"""純函式單元測試：academic_year mapping + period_label mapping。"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from models.year_end import SpecialBonusType
from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalSummary,
    Semester,
    RoleGroup,
    Grade,
    SummaryStatus,
    CycleStatus,
)
from models.employee import Employee
from services.year_end.appraisal_sync import (
    civil_year_to_target_academic_year,
    map_bonus_type_to_period_label,
    resolve_target_cycles,
    preview_payout,
    PayoutPreviewRow,
)


@pytest.mark.parametrize(
    "civil_year,expected_academic_year",
    [
        (2024, 112),
        (2025, 113),
        (2026, 114),
        (2027, 115),
        (2028, 116),
    ],
)
def test_civil_year_to_target_academic_year(civil_year, expected_academic_year):
    """payout 發放國曆年 N → 對應本學年 (N - 1911 - 1)。"""
    assert civil_year_to_target_academic_year(civil_year) == expected_academic_year


def test_map_bonus_type_to_period_label_first_is_earlier():
    """FIRST = 較早 = 前一學年上學期 → label 'N-1上'（決策②）"""
    assert (
        map_bonus_type_to_period_label(
            SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
            target_academic_year=114,
        )
        == "113上"
    )


def test_map_bonus_type_to_period_label_second_is_later():
    """SECOND = 較晚 = 前一學年下學期 → label 'N-1下'（決策②）"""
    assert (
        map_bonus_type_to_period_label(
            SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
            target_academic_year=114,
        )
        == "113下"
    )


def test_map_bonus_type_to_period_label_rejects_non_appraisal_type():
    with pytest.raises(ValueError):
        map_bonus_type_to_period_label(
            SpecialBonusType.SEMESTER_DIVIDEND_FIRST,
            target_academic_year=114,
        )


# --- DB-touching tests for resolve_target_cycles + preview_payout ---


@pytest.fixture
def sample_active_employee(test_db_session):
    """ACTIVE 員工（is_active=True）。"""
    emp = Employee(
        employee_id="E_T3_001",
        name="林老師",
        id_number="A123456789",
        hire_date=date(2024, 8, 1),
        is_active=True,
    )
    test_db_session.add(emp)
    test_db_session.flush()
    return emp


@pytest.fixture
def two_appraisal_cycles(test_db_session):
    """建 academic_year=113 FIRST(上) + academic_year=113 SECOND(下) 兩 cycle 都 CLOSED（決策②）。"""
    earlier = AppraisalCycle(
        academic_year=113,
        semester=Semester.FIRST,
        start_date=date(2024, 8, 1),
        end_date=date(2025, 1, 31),
        base_score_calc_date=date(2024, 9, 15),
        base_score=Decimal("100"),
        status=CycleStatus.CLOSED,
    )
    later = AppraisalCycle(
        academic_year=113,
        semester=Semester.SECOND,
        start_date=date(2025, 2, 1),
        end_date=date(2025, 7, 31),
        base_score_calc_date=date(2025, 2, 15),
        base_score=Decimal("100"),
        status=CycleStatus.CLOSED,
    )
    test_db_session.add_all([earlier, later])
    test_db_session.flush()
    return earlier, later


def test_resolve_target_cycles_returns_earlier_then_later(
    test_db_session, two_appraisal_cycles
):
    earlier_expected, later_expected = two_appraisal_cycles
    earlier, later = resolve_target_cycles(test_db_session, payout_year=2026)
    assert earlier.id == earlier_expected.id
    assert earlier.semester == Semester.FIRST
    assert earlier.academic_year == 113
    assert later.id == later_expected.id
    assert later.semester == Semester.SECOND
    assert later.academic_year == 113


def test_resolve_target_cycles_raises_when_cycle_missing(test_db_session):
    """113.上 或 113.下 不存在 → LookupError（決策②）。"""
    with pytest.raises(LookupError) as exc:
        resolve_target_cycles(test_db_session, payout_year=2026)
    assert "113" in str(exc.value)


def test_resolve_target_cycles_prev_full_year(test_db_session):
    """決策②：payout 2026 → 前一完整學年 113上(FIRST) + 113下(SECOND)，不含 114。"""
    # Seed 前一完整學年 113上 + 113下
    c1 = AppraisalCycle(
        academic_year=113,
        semester=Semester.FIRST,
        start_date=date(2024, 8, 1),
        end_date=date(2025, 1, 31),
        base_score_calc_date=date(2024, 9, 15),
        base_score=Decimal("100"),
        status=CycleStatus.CLOSED,
    )
    c2 = AppraisalCycle(
        academic_year=113,
        semester=Semester.SECOND,
        start_date=date(2025, 2, 1),
        end_date=date(2025, 7, 31),
        base_score_calc_date=date(2025, 2, 15),
        base_score=Decimal("100"),
        status=CycleStatus.CLOSED,
    )
    test_db_session.add_all([c1, c2])
    test_db_session.flush()

    earlier, later = resolve_target_cycles(test_db_session, payout_year=2026)

    # earlier = 113上（FIRST），later = 113下（SECOND）
    assert earlier.academic_year == 113
    assert earlier.semester == Semester.FIRST
    assert later.academic_year == 113
    assert later.semester == Semester.SECOND
    # 兩者都是 113，不包含 114
    assert earlier.academic_year != 114
    assert later.academic_year != 114


def test_preview_payout_returns_active_employee_with_both_summaries(
    test_db_session, two_appraisal_cycles, sample_active_employee
):
    """ACTIVE 員工兩 cycle 都有 finalized summary → preview 一筆，total = earlier + later。"""
    earlier, later = two_appraisal_cycles
    p1 = AppraisalParticipant(
        cycle_id=earlier.id,
        employee_id=sample_active_employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
    )
    p2 = AppraisalParticipant(
        cycle_id=later.id,
        employee_id=sample_active_employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
    )
    test_db_session.add_all([p1, p2])
    test_db_session.flush()
    s1 = AppraisalSummary(
        participant_id=p1.id,
        cycle_id=earlier.id,
        base_score=Decimal("100"),
        total_score=Decimal("80"),
        grade=Grade.GOOD,
        bonus_amount=Decimal("6400"),
        status=SummaryStatus.FINALIZED,
    )
    s2 = AppraisalSummary(
        participant_id=p2.id,
        cycle_id=later.id,
        base_score=Decimal("100"),
        total_score=Decimal("90"),
        grade=Grade.OUTSTANDING,
        bonus_amount=Decimal("7200"),
        status=SummaryStatus.FINALIZED,
    )
    test_db_session.add_all([s1, s2])
    test_db_session.flush()

    rows = preview_payout(test_db_session, payout_year=2026)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, PayoutPreviewRow)
    assert row.employee_id == sample_active_employee.id
    assert row.earlier_amount == Decimal("6400")
    assert row.later_amount == Decimal("7200")
    assert row.total_amount == Decimal("13600")
    assert row.is_inactive is False
    assert row.earlier_cycle_finalized is True
    assert row.later_cycle_finalized is True
    assert row.warnings == []


def test_preview_payout_skips_excluded_participant(
    test_db_session, two_appraisal_cycles, sample_active_employee
):
    """is_excluded=True → preview 不列出此員工。"""
    earlier, _later = two_appraisal_cycles
    p = AppraisalParticipant(
        cycle_id=earlier.id,
        employee_id=sample_active_employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
        is_excluded=True,
        exclude_reason="到職未滿三個月",
    )
    test_db_session.add(p)
    test_db_session.flush()
    rows = preview_payout(test_db_session, payout_year=2026)
    assert all(r.employee_id != sample_active_employee.id for r in rows)


def test_preview_payout_one_cycle_only_marks_warning(
    test_db_session, two_appraisal_cycles, sample_active_employee
):
    """員工只在 later cycle 出現 → earlier_amount=0 + warning。"""
    _earlier, later = two_appraisal_cycles
    p = AppraisalParticipant(
        cycle_id=later.id,
        employee_id=sample_active_employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
    )
    test_db_session.add(p)
    test_db_session.flush()
    s = AppraisalSummary(
        participant_id=p.id,
        cycle_id=later.id,
        base_score=Decimal("100"),
        total_score=Decimal("85"),
        grade=Grade.GOOD,
        bonus_amount=Decimal("5400"),
        status=SummaryStatus.FINALIZED,
    )
    test_db_session.add(s)
    test_db_session.flush()
    rows = preview_payout(test_db_session, payout_year=2026)
    assert len(rows) == 1
    assert rows[0].earlier_amount == Decimal("0")
    assert rows[0].later_amount == Decimal("5400")
    assert "not_participated_in_earlier" in rows[0].warnings


# === Task 4: generate_payouts + void_payouts tests ===

from models.year_end import SpecialBonusItem, YearEndCycle  # noqa: E402
from services.year_end.appraisal_sync import (  # noqa: E402
    GenerateResult,
    generate_payouts,
    void_payouts,
)


@pytest.fixture
def sample_resigned_employee(test_db_session):
    emp = Employee(
        employee_id="E_T4_002",
        name="陳離職",
        id_number="A222222222",
        hire_date=date(2024, 8, 1),
        is_active=False,
    )
    test_db_session.add(emp)
    test_db_session.flush()
    return emp


@pytest.fixture
def setup_summaries_for_both_employees(
    test_db_session,
    two_appraisal_cycles,
    sample_active_employee,
    sample_resigned_employee,
):
    """為 active + resigned 員工各建兩 cycle finalized summaries。"""
    earlier, later = two_appraisal_cycles
    for emp in [sample_active_employee, sample_resigned_employee]:
        p1 = AppraisalParticipant(
            cycle_id=earlier.id,
            employee_id=emp.id,
            role_group=RoleGroup.HEAD_TEACHER,
            hire_months_in_cycle=Decimal("6"),
        )
        p2 = AppraisalParticipant(
            cycle_id=later.id,
            employee_id=emp.id,
            role_group=RoleGroup.HEAD_TEACHER,
            hire_months_in_cycle=Decimal("6"),
        )
        test_db_session.add_all([p1, p2])
        test_db_session.flush()
        test_db_session.add_all(
            [
                AppraisalSummary(
                    participant_id=p1.id,
                    cycle_id=earlier.id,
                    base_score=Decimal("100"),
                    total_score=Decimal("80"),
                    grade=Grade.GOOD,
                    bonus_amount=Decimal("6400"),
                    status=SummaryStatus.FINALIZED,
                ),
                AppraisalSummary(
                    participant_id=p2.id,
                    cycle_id=later.id,
                    base_score=Decimal("100"),
                    total_score=Decimal("90"),
                    grade=Grade.OUTSTANDING,
                    bonus_amount=Decimal("7200"),
                    status=SummaryStatus.FINALIZED,
                ),
            ]
        )
        test_db_session.flush()


def test_generate_payouts_active_only_by_default(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_active_employee,
    sample_resigned_employee,
):
    result = generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    assert isinstance(result, GenerateResult)
    cycle = test_db_session.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == 114)
    )
    assert cycle is not None
    items = test_db_session.scalars(
        select(SpecialBonusItem).where(
            SpecialBonusItem.year_end_cycle_id == cycle.id,
            SpecialBonusItem.bonus_type.in_(
                [
                    SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                    SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
                ]
            ),
        )
    ).all()
    emp_ids = {i.employee_id for i in items}
    assert sample_active_employee.id in emp_ids
    assert sample_resigned_employee.id not in emp_ids


def test_generate_payouts_includes_inactive_when_selected(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_resigned_employee,
):
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids={sample_resigned_employee.id},
        generated_by=1,
    )
    cycle = test_db_session.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == 114)
    )
    items = test_db_session.scalars(
        select(SpecialBonusItem).where(SpecialBonusItem.year_end_cycle_id == cycle.id)
    ).all()
    assert sample_resigned_employee.id in {i.employee_id for i in items}


def test_generate_payouts_idempotent(
    test_db_session,
    setup_summaries_for_both_employees,
):
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    test_db_session.flush()
    count_first = test_db_session.scalar(
        select(func.count()).select_from(SpecialBonusItem)
    )

    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    test_db_session.flush()
    count_second = test_db_session.scalar(
        select(func.count()).select_from(SpecialBonusItem)
    )
    assert count_first == count_second


def test_generate_payouts_writes_source_ref_and_calc_meta(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_active_employee,
):
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    item = test_db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
        )
    )
    assert item is not None
    assert item.source_ref.startswith("appraisal_summary:")
    assert "cycle_not_finalized" in item.calc_meta
    assert "summary_status" in item.calc_meta


def test_generate_payouts_writes_appraisal_cycle_id_in_calc_meta(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_active_employee,
    two_appraisal_cycles,
):
    """calc_meta.appraisal_cycle_id 是真正的 AppraisalCycle.id（不是 AppraisalSummary.id）。"""
    earlier_cycle, later_cycle = two_appraisal_cycles
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )

    first = test_db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
        )
    )
    second = test_db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
        )
    )
    assert first is not None
    assert second is not None
    assert first.calc_meta["appraisal_cycle_id"] == earlier_cycle.id
    assert second.calc_meta["appraisal_cycle_id"] == later_cycle.id


def test_void_payouts_deletes_only_appraisal_half_bonus_items(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_active_employee,
):
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    cycle = test_db_session.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == 114)
    )

    # 模擬一筆非 APPRAISAL_HALF 的 special_bonus_item
    test_db_session.add(
        SpecialBonusItem(
            year_end_cycle_id=cycle.id,
            employee_id=sample_active_employee.id,
            bonus_type=SpecialBonusType.SEMESTER_DIVIDEND_FIRST,
            period_label="114上",
            amount=Decimal("500"),
        )
    )
    test_db_session.flush()

    deleted = void_payouts(test_db_session, payout_year=2026, voided_by=1)
    remaining = test_db_session.scalars(
        select(SpecialBonusItem).where(SpecialBonusItem.year_end_cycle_id == cycle.id)
    ).all()
    # active 員工 2 筆 (FIRST+SECOND) 都被刪
    assert deleted == 2
    assert len(remaining) == 1
    assert remaining[0].bonus_type == SpecialBonusType.SEMESTER_DIVIDEND_FIRST


# === Task B3（RA-L14）：generate_payouts 後標 2 月薪資 stale ===

from models.database import SalaryRecord  # noqa: E402


def test_generate_payouts_does_not_mark_february_salary_stale(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_active_employee,
):
    """決策⑥B 後考核獎金走 year_end settlement 表外發放，不進月薪資，
    故 generate_payouts 不再標記 2 月薪資 needs_recalc（B3 已移除）。
    """
    # 2026/2 未封存薪資，needs_recalc 起始 False
    sr = SalaryRecord(
        employee_id=sample_active_employee.id,
        salary_year=2026,
        salary_month=2,
        is_finalized=False,
        needs_recalc=False,
    )
    test_db_session.add(sr)
    test_db_session.flush()

    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )

    test_db_session.refresh(sr)
    assert sr.needs_recalc is False, "決策⑥B 後不標 stale：考核年終表外發放不進月薪"


def test_generate_payouts_does_not_touch_finalized_february(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_active_employee,
):
    """已封存 2 月薪資不被標 stale（決策⑥B 後 B3 已移除，任何月份皆不標）。"""
    sr = SalaryRecord(
        employee_id=sample_active_employee.id,
        salary_year=2026,
        salary_month=2,
        is_finalized=True,
        needs_recalc=False,
    )
    test_db_session.add(sr)
    test_db_session.flush()

    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )

    test_db_session.refresh(sr)
    assert sr.needs_recalc is False, "封存薪資不被標 stale（B3 已移除亦同）"


def test_generate_payouts_does_not_touch_january(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_active_employee,
):
    """1 月薪資不被標 stale（決策⑥B 後 B3 已移除，任何月份皆不標）。"""
    sr = SalaryRecord(
        employee_id=sample_active_employee.id,
        salary_year=2026,
        salary_month=1,
        is_finalized=False,
        needs_recalc=False,
    )
    test_db_session.add(sr)
    test_db_session.flush()

    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )

    test_db_session.refresh(sr)
    assert sr.needs_recalc is False, "1 月薪資不被標 stale（B3 已移除）"


# === P2（2026-06-16）：payout generate/void 不可改動已簽核年終的明細 ===
#
# 威脅：generate_payouts/void_payouts 只動 SpecialBonusItem（APPRAISAL_HALF_*），
# 完全不檢查對應 YearEndSettlement.status。若年終已簽核/核定，settlement.total_amount
# 已凍結（build_settlements 對非 DRAFT skip），但 payout 仍可覆寫/硬刪明細
# → 匯出總表(讀 settlement.total)與明細條(讀 items)對不起來。

from models.year_end import (  # noqa: E402
    EmployeeYearEndSnapshot,
    YearEndSettlement,
    YearEndSettlementStatus,
)


def _make_frozen_settlement(
    session,
    cycle_id: int,
    employee_id: int,
    status: YearEndSettlementStatus,
) -> YearEndSettlement:
    """為指定 (year_end_cycle, employee) 建立一張已簽核/核定的 settlement。"""
    snap = EmployeeYearEndSnapshot(
        year_end_cycle_id=cycle_id,
        employee_id=employee_id,
        base_salary=Decimal("40000"),
        festival_total=Decimal("0"),
        hire_months=Decimal("12"),
    )
    session.add(snap)
    session.flush()
    s = YearEndSettlement(
        year_end_cycle_id=cycle_id,
        employee_id=employee_id,
        snapshot_id=snap.id,
        payable_amount=Decimal("50000"),
        total_amount=Decimal("50000"),
        special_bonus_total=Decimal("0"),
        status=status,
    )
    session.add(s)
    session.flush()
    return s


def test_generate_payouts_skips_frozen_settlement(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_active_employee,
):
    """已簽核 settlement 的員工：重跑 generate 不得覆寫其 APPRAISAL_HALF 金額。"""
    # 首次生成（active 員工 earlier=6400 / later=7200）
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    cycle = test_db_session.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == 114)
    )
    first_item = test_db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
        )
    )
    assert first_item.amount == Decimal("6400")

    # 該員工年終結算已主管簽核（凍結）
    _make_frozen_settlement(
        test_db_session,
        cycle.id,
        sample_active_employee.id,
        YearEndSettlementStatus.SUPERVISOR_SIGNED,
    )

    # 考核金額事後被改（模擬重算），再跑一次 generate
    earlier_summary = test_db_session.scalar(
        select(AppraisalSummary)
        .join(
            AppraisalParticipant,
            AppraisalSummary.participant_id == AppraisalParticipant.id,
        )
        .where(
            AppraisalParticipant.employee_id == sample_active_employee.id,
            AppraisalSummary.bonus_amount == Decimal("6400"),
        )
    )
    earlier_summary.bonus_amount = Decimal("9999")
    test_db_session.flush()

    result = generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )

    test_db_session.refresh(first_item)
    # 凍結 → 金額不得被覆寫成 9999
    assert first_item.amount == Decimal("6400"), "已簽核年終明細被 generate 覆寫"
    # 該員工被視為 frozen-skip，warnings 標記
    assert any(
        str(sample_active_employee.id) in w and "frozen" in w for w in result.warnings
    ), f"frozen-skip 未反映在 warnings: {result.warnings}"


def test_void_payouts_skips_frozen_settlement(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_active_employee,
    sample_resigned_employee,
):
    """已簽核 settlement 的員工：void 不得硬刪其 APPRAISAL_HALF 明細；
    未簽核(無 settlement)的員工照常刪除。"""
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids={sample_resigned_employee.id},
        generated_by=1,
    )
    cycle = test_db_session.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == 114)
    )
    # active 員工年終已會計簽核（凍結）；resigned 員工無 settlement（可刪）
    _make_frozen_settlement(
        test_db_session,
        cycle.id,
        sample_active_employee.id,
        YearEndSettlementStatus.ACCOUNTING_SIGNED,
    )

    deleted = void_payouts(test_db_session, payout_year=2026, voided_by=1)

    active_items = test_db_session.scalars(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type.in_(
                [
                    SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                    SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
                ]
            ),
        )
    ).all()
    resigned_items = test_db_session.scalars(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_resigned_employee.id
        )
    ).all()
    # 凍結員工 2 筆保留；非凍結員工 2 筆刪除
    assert len(active_items) == 2, "已簽核年終明細被 void 硬刪"
    assert len(resigned_items) == 0
    assert deleted == 2


# === P3（2026-06-16）：generate payout 的審計資訊不可誤導 ===
#
# 威脅：generate_payouts 回傳寫死 warnings=[]，且 calc_meta["summary_status"] 只要有
# summary id 就寫 "FINALIZED"——未核定 summary 被標成已核定，且 preview 已偵測到的
# warning 被吞掉。


def test_generate_payouts_calc_meta_reflects_unfinalized_summary(
    test_db_session,
    two_appraisal_cycles,
    sample_active_employee,
):
    """earlier summary 未核定（DRAFT）→ calc_meta.summary_status 應為 NOT_FINALIZED，
    且 result.warnings 帶出 preview 的 earlier_summary_not_finalized。"""
    earlier, later = two_appraisal_cycles
    p1 = AppraisalParticipant(
        cycle_id=earlier.id,
        employee_id=sample_active_employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
    )
    p2 = AppraisalParticipant(
        cycle_id=later.id,
        employee_id=sample_active_employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
    )
    test_db_session.add_all([p1, p2])
    test_db_session.flush()
    test_db_session.add_all(
        [
            # earlier 未核定
            AppraisalSummary(
                participant_id=p1.id,
                cycle_id=earlier.id,
                base_score=Decimal("100"),
                total_score=Decimal("80"),
                grade=Grade.GOOD,
                bonus_amount=Decimal("6400"),
                status=SummaryStatus.DRAFT,
            ),
            # later 已核定
            AppraisalSummary(
                participant_id=p2.id,
                cycle_id=later.id,
                base_score=Decimal("100"),
                total_score=Decimal("90"),
                grade=Grade.OUTSTANDING,
                bonus_amount=Decimal("7200"),
                status=SummaryStatus.FINALIZED,
            ),
        ]
    )
    test_db_session.flush()

    result = generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )

    first = test_db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
        )
    )
    second = test_db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
        )
    )
    # earlier 未核定 → 不可標成 FINALIZED
    assert first.calc_meta["summary_status"] == "NOT_FINALIZED"
    # later 已核定 → FINALIZED
    assert second.calc_meta["summary_status"] == "FINALIZED"
    # preview 偵測到的 warning 不可被吞
    assert "earlier_summary_not_finalized" in result.warnings


def test_generate_payouts_calc_meta_missing_summary(
    test_db_session,
    two_appraisal_cycles,
    sample_active_employee,
):
    """員工只在 earlier 參與（later 無 summary）→ later 那筆 summary_status=MISSING。"""
    earlier, later = two_appraisal_cycles
    p1 = AppraisalParticipant(
        cycle_id=earlier.id,
        employee_id=sample_active_employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
    )
    test_db_session.add(p1)
    test_db_session.flush()
    test_db_session.add(
        AppraisalSummary(
            participant_id=p1.id,
            cycle_id=earlier.id,
            base_score=Decimal("100"),
            total_score=Decimal("80"),
            grade=Grade.GOOD,
            bonus_amount=Decimal("6400"),
            status=SummaryStatus.FINALIZED,
        )
    )
    test_db_session.flush()

    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )

    second = test_db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
        )
    )
    assert second.calc_meta["summary_status"] == "MISSING"
    assert second.amount == Decimal("0")


# === #9 + #10（2026-06-16）：未定案績效不得流入可轉帳金額 ===
#
# 威脅：generate_payouts 直接把 AppraisalSummary.bonus_amount 寫進 SpecialBonusItem.amount，
# 完全不看 summary.status。若某半年考核 summary 還是 DRAFT（未定案），其金額會直接流入
# 年終可轉帳金額（settlement.total_amount）。fail-safe：未定案績效金額一律視為 0/跳過寫入，
# 與 year_end excel_io 既有 fail-safe 風格一致（不硬性 422 阻擋整個端點）。


def test_generate_payouts_zeroes_amount_for_unfinalized_summary(
    test_db_session,
    two_appraisal_cycles,
    sample_active_employee,
):
    """earlier summary 未定案（DRAFT）→ 該半年 payout 金額必須視為 0（不可寫入 6400）。"""
    earlier, later = two_appraisal_cycles
    p1 = AppraisalParticipant(
        cycle_id=earlier.id,
        employee_id=sample_active_employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
    )
    p2 = AppraisalParticipant(
        cycle_id=later.id,
        employee_id=sample_active_employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
    )
    test_db_session.add_all([p1, p2])
    test_db_session.flush()
    test_db_session.add_all(
        [
            # earlier 未定案（DRAFT）→ 金額不得流入
            AppraisalSummary(
                participant_id=p1.id,
                cycle_id=earlier.id,
                base_score=Decimal("100"),
                total_score=Decimal("80"),
                grade=Grade.GOOD,
                bonus_amount=Decimal("6400"),
                status=SummaryStatus.DRAFT,
            ),
            # later 已定案 → 正常入帳
            AppraisalSummary(
                participant_id=p2.id,
                cycle_id=later.id,
                base_score=Decimal("100"),
                total_score=Decimal("90"),
                grade=Grade.OUTSTANDING,
                bonus_amount=Decimal("7200"),
                status=SummaryStatus.FINALIZED,
            ),
        ]
    )
    test_db_session.flush()

    result = generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )

    first = test_db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
        )
    )
    second = test_db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
        )
    )
    # earlier 未定案 → 金額視為 0（fail-safe，不可流入可轉帳金額）
    assert first.amount == Decimal("0"), "未定案績效金額被寫入可轉帳明細"
    assert first.calc_meta["summary_status"] == "NOT_FINALIZED"
    # later 已定案 → 正常 7200
    assert second.amount == Decimal("7200")
    # result.total 只計入已定案的 7200
    assert result.total_amount == Decimal("7200")
    # 仍帶出 warning
    assert "earlier_summary_not_finalized" in result.warnings


def test_generate_payouts_does_not_hard_block_on_unfinalized(
    test_db_session,
    two_appraisal_cycles,
    sample_active_employee,
):
    """fail-safe：未定案 summary 不得硬性 raise/422 阻擋整個 generate（與 excel_io 風格一致）。

    即使兩半年皆未定案，generate 仍應正常返回（金額 0），而非拋例外。
    """
    earlier, later = two_appraisal_cycles
    p1 = AppraisalParticipant(
        cycle_id=earlier.id,
        employee_id=sample_active_employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
    )
    p2 = AppraisalParticipant(
        cycle_id=later.id,
        employee_id=sample_active_employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
    )
    test_db_session.add_all([p1, p2])
    test_db_session.flush()
    test_db_session.add_all(
        [
            AppraisalSummary(
                participant_id=p1.id,
                cycle_id=earlier.id,
                base_score=Decimal("100"),
                total_score=Decimal("80"),
                grade=Grade.GOOD,
                bonus_amount=Decimal("6400"),
                status=SummaryStatus.DRAFT,
            ),
            AppraisalSummary(
                participant_id=p2.id,
                cycle_id=later.id,
                base_score=Decimal("100"),
                total_score=Decimal("90"),
                grade=Grade.OUTSTANDING,
                bonus_amount=Decimal("7200"),
                status=SummaryStatus.DRAFT,
            ),
        ]
    )
    test_db_session.flush()

    # 不應拋例外
    result = generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    assert result.total_amount == Decimal("0")
    first = test_db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
        )
    )
    second = test_db_session.scalar(
        select(SpecialBonusItem).where(
            SpecialBonusItem.employee_id == sample_active_employee.id,
            SpecialBonusItem.bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
        )
    )
    assert first.amount == Decimal("0")
    assert second.amount == Decimal("0")


# === #1（2026-06-16）：generate/void 後重算 DRAFT settlement 的 total ===
#
# 威脅：generate_payouts / void_payouts 只動 SpecialBonusItem，不更新對應
# YearEndSettlement.special_bonus_total / total_amount。若該員工 settlement 仍是
# DRAFT（未凍結），轉帳名冊（讀 settlement.total_amount）與明細條（即時 aggregate
# SpecialBonusItem）會對不起來。凍結（非 DRAFT）員工沿用「跳過不改」（步驟 A），
# 此處只負責 DRAFT 員工的同步重算。


def _make_draft_settlement(
    session,
    cycle_id: int,
    employee_id: int,
    payable: Decimal = Decimal("50000"),
) -> YearEndSettlement:
    """為 (cycle, employee) 建一張 DRAFT settlement（special_bonus_total 起始 0）。"""
    snap = EmployeeYearEndSnapshot(
        year_end_cycle_id=cycle_id,
        employee_id=employee_id,
        base_salary=Decimal("40000"),
        festival_total=Decimal("0"),
        hire_months=Decimal("12"),
    )
    session.add(snap)
    session.flush()
    s = YearEndSettlement(
        year_end_cycle_id=cycle_id,
        employee_id=employee_id,
        snapshot_id=snap.id,
        payable_amount=payable,
        total_amount=payable,
        special_bonus_total=Decimal("0"),
        status=YearEndSettlementStatus.DRAFT,
    )
    session.add(s)
    session.flush()
    return s


def test_generate_payouts_recomputes_draft_settlement_total(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_active_employee,
):
    """generate 後，DRAFT settlement.total_amount 必須等於 payable + SUM(special_bonus)。"""
    # 先用一次 generate 取得 cycle，再為 active 員工建 DRAFT settlement
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    cycle = test_db_session.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == 114)
    )
    settlement = _make_draft_settlement(
        test_db_session, cycle.id, sample_active_employee.id
    )
    # 此時 settlement 還沒反映剛剛生成的 6400+7200
    assert settlement.special_bonus_total == Decimal("0")
    assert settlement.total_amount == Decimal("50000")

    # 再跑一次 generate（idempotent；金額不變但須同步 settlement）
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )

    # 重算後：special_bonus_total = 6400 + 7200 = 13600；total = 50000 + 13600
    sb_sum = test_db_session.scalar(
        select(func.coalesce(func.sum(SpecialBonusItem.amount), 0)).where(
            SpecialBonusItem.year_end_cycle_id == cycle.id,
            SpecialBonusItem.employee_id == sample_active_employee.id,
        )
    )
    test_db_session.refresh(settlement)
    assert settlement.special_bonus_total == Decimal(str(sb_sum))
    assert settlement.special_bonus_total == Decimal("13600")
    assert settlement.total_amount == settlement.payable_amount + Decimal(str(sb_sum))
    assert settlement.total_amount == Decimal("63600")


def test_void_payouts_recomputes_draft_settlement_total(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_active_employee,
):
    """void 後，DRAFT settlement.total_amount 必須回到 payable（special bonus 已刪）。"""
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    cycle = test_db_session.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == 114)
    )
    settlement = _make_draft_settlement(
        test_db_session, cycle.id, sample_active_employee.id
    )
    # 先讓 settlement 反映現有 special bonus
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    test_db_session.refresh(settlement)
    assert settlement.total_amount == Decimal("63600")

    # void 全部 APPRAISAL_HALF → DRAFT settlement total 應回到 payable
    void_payouts(test_db_session, payout_year=2026, voided_by=1)

    test_db_session.refresh(settlement)
    sb_sum = test_db_session.scalar(
        select(func.coalesce(func.sum(SpecialBonusItem.amount), 0)).where(
            SpecialBonusItem.year_end_cycle_id == cycle.id,
            SpecialBonusItem.employee_id == sample_active_employee.id,
        )
    )
    assert settlement.special_bonus_total == Decimal(str(sb_sum))
    assert settlement.special_bonus_total == Decimal("0")
    assert settlement.total_amount == settlement.payable_amount
    assert settlement.total_amount == Decimal("50000")


def test_generate_payouts_does_not_recompute_frozen_settlement_total(
    test_db_session,
    setup_summaries_for_both_employees,
    sample_active_employee,
):
    """凍結（非 DRAFT）settlement：generate 既不改明細也不改其 total（沿用步驟 A）。"""
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    cycle = test_db_session.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == 114)
    )
    # 凍結 settlement，total 寫一個與 special bonus 不一致的「定案值」
    frozen = _make_frozen_settlement(
        test_db_session,
        cycle.id,
        sample_active_employee.id,
        YearEndSettlementStatus.ACCOUNTING_SIGNED,
    )
    frozen.total_amount = Decimal("99999")
    frozen.special_bonus_total = Decimal("0")
    test_db_session.flush()

    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )

    test_db_session.refresh(frozen)
    # 凍結 → total 不被重算（沿用定案值）
    assert frozen.total_amount == Decimal("99999")
    assert frozen.special_bonus_total == Decimal("0")
