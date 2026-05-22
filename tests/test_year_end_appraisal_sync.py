"""純函式單元測試：academic_year mapping + period_label mapping。"""

from datetime import date
from decimal import Decimal

import pytest

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
    """FIRST = 較早 = 前一學年下學期 → label 'N-1下'"""
    assert (
        map_bonus_type_to_period_label(
            SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
            target_academic_year=114,
        )
        == "113下"
    )


def test_map_bonus_type_to_period_label_second_is_later():
    """SECOND = 較晚 = 本學年上學期 → label 'N上'"""
    assert (
        map_bonus_type_to_period_label(
            SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
            target_academic_year=114,
        )
        == "114上"
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
    """建 academic_year=113 SECOND + academic_year=114 FIRST 兩 cycle 都 CLOSED。"""
    earlier = AppraisalCycle(
        academic_year=113,
        semester=Semester.SECOND,
        start_date=date(2025, 2, 1),
        end_date=date(2025, 7, 31),
        base_score_calc_date=date(2025, 2, 15),
        base_score=Decimal("100"),
        status=CycleStatus.CLOSED,
    )
    later = AppraisalCycle(
        academic_year=114,
        semester=Semester.FIRST,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
        base_score_calc_date=date(2025, 9, 15),
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
    assert earlier.semester == Semester.SECOND
    assert earlier.academic_year == 113
    assert later.id == later_expected.id
    assert later.semester == Semester.FIRST
    assert later.academic_year == 114


def test_resolve_target_cycles_raises_when_cycle_missing(test_db_session):
    """113.下 或 114.上 不存在 → LookupError。"""
    with pytest.raises(LookupError) as exc:
        resolve_target_cycles(test_db_session, payout_year=2026)
    assert "113" in str(exc.value) or "114" in str(exc.value)


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
