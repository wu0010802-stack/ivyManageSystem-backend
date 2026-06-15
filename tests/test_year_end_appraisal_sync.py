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


# === P2-4（2026-06-15 運作探測）：payout 硬閘 + calc_meta 誠實 ===


def test_generate_payouts_blocks_when_summary_not_finalized(
    test_db_session, two_appraisal_cycles, sample_active_employee
):
    """有 summary 未 FINALIZED 時 generate_payouts 須拒絕（防跳過三簽發放考核年終）。

    舊行為：generate_payouts 不檢查 summary 狀態，照 amount 寫 payout，且
    calc_meta.summary_status 對任何存在的 summary 硬寫 'FINALIZED' 謊報稽核。
    """
    from services.year_end.appraisal_sync import PayoutNotFinalizedError

    earlier, later = two_appraisal_cycles
    emp = sample_active_employee
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
                status=SummaryStatus.SUPERVISOR_SIGNED,  # 未走完三簽
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

    with pytest.raises(PayoutNotFinalizedError):
        generate_payouts(
            test_db_session,
            payout_year=2026,
            included_inactive_employee_ids=set(),
            generated_by=1,
        )
    # 硬閘在寫入前觸發，不可有任何 payout 落地
    items = test_db_session.scalars(select(SpecialBonusItem)).all()
    assert items == []


def test_generate_payouts_calc_meta_summary_status_truthful(
    test_db_session, setup_summaries_for_both_employees
):
    """全 FINALIZED 成功產生時，calc_meta.summary_status 須反映真實（present→FINALIZED）。"""
    generate_payouts(
        test_db_session,
        payout_year=2026,
        included_inactive_employee_ids=set(),
        generated_by=1,
    )
    items = test_db_session.scalars(select(SpecialBonusItem)).all()
    assert items
    for it in items:
        if it.source_ref and it.source_ref != "appraisal_summary:none":
            assert it.calc_meta["summary_status"] == "FINALIZED"
        else:
            assert it.calc_meta["summary_status"] == "MISSING"
