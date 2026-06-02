"""Salary engine plugin: appraisal_year_end query helper。

Plugin layer 測試（不跑完整 salary engine pipeline，只測 query helper 正確性）。

注意：決策⑥B (2026-06-02) 後，query_appraisal_year_end_bonus / bulk 函式本身仍正確
（standalone 測試繼續存在）；但 engine 不再呼叫它們。
engine 行為守衛測試見本檔最下方 test_salary_engine_does_not_call_appraisal_year_end_plugin
與 test_engine_does_not_pull_appraisal_in_february。
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
                period_label="113上",
                amount=Decimal("6400"),
            ),
            SpecialBonusItem(
                year_end_cycle_id=cycle.id,
                employee_id=sample_active_employee_t5.id,
                bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
                period_label="113下",
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
            period_label="113上",
            amount=Decimal("9999"),
        )
    )
    test_db_session.add(
        SpecialBonusItem(
            year_end_cycle_id=cycle_113.id,
            employee_id=sample_active_employee_t5.id,
            bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
            period_label="112上",
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


# === 決策⑥B regression guard: salary engine 不再呼叫 appraisal_year_end plugin ===


def test_salary_engine_does_not_call_appraisal_year_end_plugin():
    """決策⑥B 守衛：確認 engine.py 不再 import 或呼叫 query_appraisal_year_end_bonus。

    engine.py 仍會寫 SalaryRecord.appraisal_year_end_bonus（值恆為 0）；
    但不可再出現 query_appraisal_year_end_bonus 的呼叫引用（避免重複發放 + 二代健保表外）。
    """
    from pathlib import Path

    engine_src = Path(__file__).parent.parent / "services" / "salary" / "engine.py"
    code = engine_src.read_text()
    # 決策⑥B：engine 不應再引用 query_appraisal_year_end_bonus
    assert "query_appraisal_year_end_bonus" not in code, (
        "engine.py 仍含 query_appraisal_year_end_bonus 引用；"
        "決策⑥B 要求 engine 不再從 appraisal_year_end 拉值（改由年終獨立轉帳）"
    )
    # column 寫入仍保留（向後相容），值恆 0
    assert (
        "appraisal_year_end_bonus" in code
    ), "engine.py 不應刪除 SalaryRecord.appraisal_year_end_bonus 的欄位寫入（向後相容）"


def test_engine_does_not_pull_appraisal_in_february(
    test_db_session, sample_active_employee_t5, cycle_with_two_payouts
):
    """決策⑖B 功能守衛：即使 APPRAISAL_HALF_BONUS 資料存在，engine 2 月薪資計算
    也應將 appraisal_year_end_bonus 填 Decimal("0")，而非拉 special_bonus_items。

    以直接呼叫 _fill_salary_record 搭配 mock session 驗證，不跑完整 pipeline。
    """
    from decimal import Decimal
    from unittest.mock import MagicMock

    from models.salary import SalaryRecord
    from services.salary.engine import _fill_salary_record, SalaryEngine
    from services.salary.breakdown import SalaryBreakdown

    # cycle_with_two_payouts fixture 已建立 APPRAISAL 兩筆 (6400+7200=13600)
    # 若 engine 仍拉資料則 appraisal_year_end_bonus == 13600；決策⑥B 後應為 0。

    salary_record = SalaryRecord(
        employee_id=sample_active_employee_t5.id,
        salary_year=2026,
        salary_month=2,
        appraisal_year_end_bonus=Decimal("0"),
        manual_overrides=[],
    )

    breakdown = SalaryBreakdown(
        employee_name="林老師",
        employee_id=str(sample_active_employee_t5.id),
        year=2026,
        month=2,
    )
    # 最小化 breakdown 以免觸碰無關欄位
    breakdown.base_salary = Decimal("40000")
    breakdown.festival_bonus = Decimal("0")
    breakdown.overtime_bonus = Decimal("0")
    breakdown.supervisor_dividend = Decimal("0")
    breakdown.performance_bonus = Decimal("0")
    breakdown.special_bonus = Decimal("0")
    breakdown.birthday_bonus = Decimal("0")
    breakdown.gross_salary = Decimal("40000")
    breakdown.total_deduction = Decimal("0")
    breakdown.net_salary = Decimal("40000")

    # 最小化 engine mock（只需 bonus_config_id / attendance_policy_id）
    mock_engine = MagicMock(spec=SalaryEngine)
    mock_engine._bonus_config_id = None
    mock_engine._attendance_policy_id = None

    _fill_salary_record(
        salary_record,
        breakdown,
        mock_engine,
        session=test_db_session,  # 真實 session，含 APPRAISAL 資料
        appraisal_bonus=None,      # 單筆路徑；舊行為：此時會 query → 決策⑥B 後不再 query
        pending_logs=[],
    )

    # 比較前轉 Decimal 確保 int/float/Decimal 皆一致（Money column 在 transient object 可能為 int）
    assert Decimal(str(salary_record.appraisal_year_end_bonus or 0)) == Decimal("0"), (
        f"engine 仍在 2 月填入非 0 的 appraisal_year_end_bonus="
        f"{salary_record.appraisal_year_end_bonus}；決策⑥B 要求恆為 0"
    )
