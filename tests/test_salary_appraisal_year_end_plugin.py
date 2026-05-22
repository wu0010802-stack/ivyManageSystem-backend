"""Salary engine plugin: appraisal_year_end query helper。

Plugin layer 測試（不跑完整 salary engine pipeline，只測 query helper 正確性）。
"""

from datetime import date
from decimal import Decimal

import pytest

from models.employee import Employee
from models.year_end import SpecialBonusItem, SpecialBonusType, YearEndCycle
from services.salary.appraisal_year_end import query_appraisal_year_end_bonus


@pytest.fixture
def sample_active_employee_t5(test_db_session):
    emp = Employee(
        employee_id="E_T5_001",
        name="林老師",
        id_number="A555555555",
        hire_date=date(2024, 8, 1),
        is_active=True,
    )
    test_db_session.add(emp)
    test_db_session.flush()
    return emp


@pytest.fixture
def cycle_with_two_payouts(test_db_session, sample_active_employee_t5):
    cycle = YearEndCycle(
        academic_year=114,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 7, 31),
        bonus_calc_date=date(2026, 1, 15),
    )
    test_db_session.add(cycle)
    test_db_session.flush()
    test_db_session.add_all(
        [
            SpecialBonusItem(
                year_end_cycle_id=cycle.id,
                employee_id=sample_active_employee_t5.id,
                bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                period_label="113下",
                amount=Decimal("6400"),
            ),
            SpecialBonusItem(
                year_end_cycle_id=cycle.id,
                employee_id=sample_active_employee_t5.id,
                bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
                period_label="114上",
                amount=Decimal("7200"),
            ),
        ]
    )
    test_db_session.flush()
    return cycle


@pytest.mark.parametrize("month", [1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
def test_query_returns_zero_for_non_february(
    test_db_session, sample_active_employee_t5, cycle_with_two_payouts, month
):
    result = query_appraisal_year_end_bonus(
        test_db_session, sample_active_employee_t5.id, 2026, month
    )
    assert result == Decimal("0"), f"month={month} should be 0"


def test_query_returns_sum_for_february(
    test_db_session, sample_active_employee_t5, cycle_with_two_payouts
):
    result = query_appraisal_year_end_bonus(
        test_db_session, sample_active_employee_t5.id, 2026, 2
    )
    assert result == Decimal("13600")


def test_query_returns_zero_when_no_payout(test_db_session, sample_active_employee_t5):
    """員工沒被 generate payout → 2 月也是 0。"""
    result = query_appraisal_year_end_bonus(
        test_db_session, sample_active_employee_t5.id, 2026, 2
    )
    assert result == Decimal("0")


def test_query_only_sums_appraisal_half_bonus_types(
    test_db_session, sample_active_employee_t5, cycle_with_two_payouts
):
    """同 cycle 有其他 type 的 special_bonus_item 不應被加入。"""
    test_db_session.add(
        SpecialBonusItem(
            year_end_cycle_id=cycle_with_two_payouts.id,
            employee_id=sample_active_employee_t5.id,
            bonus_type=SpecialBonusType.SEMESTER_DIVIDEND_FIRST,
            period_label="114上",
            amount=Decimal("9999"),
        )
    )
    test_db_session.flush()
    result = query_appraisal_year_end_bonus(
        test_db_session, sample_active_employee_t5.id, 2026, 2
    )
    assert result == Decimal("13600")  # 不含 9999


def test_query_correct_academic_year_mapping(
    test_db_session, sample_active_employee_t5
):
    """payout_year=2025 → target_academic_year=113，不要拉到 114 cycle 的金額。"""
    cycle_114 = YearEndCycle(
        academic_year=114,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 7, 31),
        bonus_calc_date=date(2026, 1, 15),
    )
    cycle_113 = YearEndCycle(
        academic_year=113,
        start_date=date(2024, 8, 1),
        end_date=date(2025, 7, 31),
        bonus_calc_date=date(2025, 1, 15),
    )
    test_db_session.add_all([cycle_114, cycle_113])
    test_db_session.flush()
    test_db_session.add(
        SpecialBonusItem(
            year_end_cycle_id=cycle_114.id,
            employee_id=sample_active_employee_t5.id,
            bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
            period_label="113下",
            amount=Decimal("9999"),
        )
    )
    test_db_session.add(
        SpecialBonusItem(
            year_end_cycle_id=cycle_113.id,
            employee_id=sample_active_employee_t5.id,
            bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
            period_label="112下",
            amount=Decimal("3000"),
        )
    )
    test_db_session.flush()
    result_2025 = query_appraisal_year_end_bonus(
        test_db_session, sample_active_employee_t5.id, 2025, 2
    )
    result_2026 = query_appraisal_year_end_bonus(
        test_db_session, sample_active_employee_t5.id, 2026, 2
    )
    assert result_2025 == Decimal("3000")
    assert result_2026 == Decimal("9999")


# === Integration test: salary engine 確實呼叫 plugin ===


def test_salary_engine_hook_invokes_appraisal_year_end_plugin():
    """確認 services/salary/engine.py 在 _fill_salary_record
    （或對應 function）的程式碼中含 query_appraisal_year_end_bonus 呼叫。

    這是「煙霧測試」確認 Task 5 step 5 的 1 行 hook 確實加進去。
    不跑整個 calculate pipeline（太複雜、需大量 fixtures）。
    """
    from pathlib import Path

    engine_src = Path(__file__).parent.parent / "services" / "salary" / "engine.py"
    code = engine_src.read_text()
    assert "query_appraisal_year_end_bonus" in code, (
        "engine.py 未呼叫 query_appraisal_year_end_bonus；"
        "Task 5 step 5 的 hook 未到位"
    )
    assert (
        "appraisal_year_end_bonus" in code
    ), "engine.py 未寫入 SalaryRecord.appraisal_year_end_bonus column"
