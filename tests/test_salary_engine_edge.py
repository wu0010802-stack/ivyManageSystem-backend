"""薪資引擎邊界條件測試。

聚焦於 CLAUDE.md 指定的關鍵規則：
- 節慶獎金發放月（2/6/9/12）判斷
- MONTHLY_BASE_DAYS=30 的時薪基準
- 會議缺席扣款期間起算日
- get_working_days 的 month 邊界驗證
"""

from datetime import date

import pytest

from services.salary.constants import MONTHLY_BASE_DAYS
from services.salary.utils import (
    calc_daily_salary,
    get_bonus_distribution_month,
    get_meeting_deduction_period_start,
    get_working_days,
)


class TestBonusDistributionMonth:
    @pytest.mark.parametrize("month", [2, 6, 9, 12])
    def test_distribution_months(self, month):
        assert get_bonus_distribution_month(month) is True

    @pytest.mark.parametrize("month", [1, 3, 4, 5, 7, 8, 10, 11])
    def test_non_distribution_months(self, month):
        assert get_bonus_distribution_month(month) is False

    def test_all_months_have_deterministic_answer(self):
        """12 月內每個月都能回答 True/False，無例外。"""
        results = [get_bonus_distribution_month(m) for m in range(1, 13)]
        assert sum(results) == 4  # 恰好 4 個發放月


class TestMeetingDeductionPeriodStart:
    def test_feb_covers_january(self):
        assert get_meeting_deduction_period_start(2026, 2) == date(2026, 1, 1)

    def test_june_covers_march_to_may(self):
        assert get_meeting_deduction_period_start(2026, 6) == date(2026, 3, 1)

    def test_september_covers_july_to_aug(self):
        assert get_meeting_deduction_period_start(2026, 9) == date(2026, 7, 1)

    def test_december_covers_oct_to_nov(self):
        assert get_meeting_deduction_period_start(2026, 12) == date(2026, 10, 1)

    @pytest.mark.parametrize("month", [1, 3, 4, 5, 7, 8, 10, 11])
    def test_non_distribution_months_return_none(self, month):
        assert get_meeting_deduction_period_start(2026, month) is None


class TestCalcDailySalary:
    def test_standard_salary(self):
        # 30000 / 30 = 1000
        assert calc_daily_salary(30000) == 1000

    def test_zero_salary(self):
        assert calc_daily_salary(0) == 0

    def test_none_salary_defaults_to_zero(self):
        """負責處理未填底薪的員工（如兼職/臨時），不應拋錯。"""
        assert calc_daily_salary(None) == 0

    def test_uses_constant_monthly_base_days(self):
        """若 MONTHLY_BASE_DAYS 變更，所有計算會同步變更。"""
        assert MONTHLY_BASE_DAYS == 30
        # 3 萬元 / 30 天 = 1000 元/天
        assert calc_daily_salary(30000) == 30000 / MONTHLY_BASE_DAYS


class TestGetWorkingDaysValidation:
    def test_invalid_month_raises(self):
        with pytest.raises(ValueError):
            get_working_days(2026, 0)
        with pytest.raises(ValueError):
            get_working_days(2026, 13)
        with pytest.raises(ValueError):
            get_working_days(2026, -1)


class TestSumLeaveDeduction:
    """驗證請假扣款使用 deduction_ratio 欄位優先。"""

    def test_uses_deduction_ratio_when_present(self):
        from services.salary.utils import _sum_leave_deduction

        class FakeLeave:
            def __init__(self, hours, ratio, leave_type="事假"):
                self.leave_hours = hours
                self.deduction_ratio = ratio
                self.leave_type = leave_type

        # 8 小時 = 1 天；1000 日薪 × ratio=0.5 = 500
        total = _sum_leave_deduction([FakeLeave(8, 0.5)], daily_salary=1000)
        assert total == 500

    def test_fallback_to_leave_type_rules_when_ratio_none(self):
        from services.salary.utils import _sum_leave_deduction
        from services.salary.constants import LEAVE_DEDUCTION_RULES

        class FakeLeave:
            def __init__(self, hours, ratio, leave_type):
                self.leave_hours = hours
                self.deduction_ratio = ratio
                self.leave_type = leave_type

        # deduction_ratio=None 時查 LEAVE_DEDUCTION_RULES
        known_type = next(iter(LEAVE_DEDUCTION_RULES))
        expected_ratio = LEAVE_DEDUCTION_RULES[known_type]
        total = _sum_leave_deduction(
            [FakeLeave(8, None, known_type)], daily_salary=1000
        )
        assert total == 1000 * expected_ratio

    def test_unknown_leave_type_with_no_ratio_defaults_to_full_deduction(self):
        from services.salary.utils import _sum_leave_deduction

        class FakeLeave:
            def __init__(self):
                self.leave_hours = 8
                self.deduction_ratio = None
                self.leave_type = "不存在的假別"

        # fallback 的 default 是 1.0（全扣）
        total = _sum_leave_deduction([FakeLeave()], daily_salary=1000)
        assert total == 1000

    def test_partial_day_leave_pro_rated(self):
        from services.salary.utils import _sum_leave_deduction

        class FakeLeave:
            def __init__(self, hours, ratio):
                self.leave_hours = hours
                self.deduction_ratio = ratio
                self.leave_type = "事假"

        # 4 小時（半天）× 1000 日薪 × 1.0 = 500
        total = _sum_leave_deduction([FakeLeave(4, 1.0)], daily_salary=1000)
        assert total == 500

    def test_empty_list_returns_zero(self):
        from services.salary.utils import _sum_leave_deduction

        assert _sum_leave_deduction([], daily_salary=1000) == 0
