"""
跨年度與保費級距邊界 行為鎖定測試。

涵蓋面向：
- 節慶獎金發放月判定
- 跨年度入職資格（滿 N 個月）
- 閏年 2 月按日比例折算
- 保費級距上下限
"""

import pytest
from datetime import date

from services.salary.utils import get_bonus_distribution_month
from services.salary.festival import is_eligible_for_festival_bonus
from services.insurance_service import InsuranceService


# ──────────────────────────────────────────────────────────────
# 節慶獎金發放月
# ──────────────────────────────────────────────────────────────
class TestBonusDistributionMonth:
    @pytest.mark.parametrize("month", [2, 6, 9, 12])
    def test_distribution_months(self, month):
        assert get_bonus_distribution_month(month) is True

    @pytest.mark.parametrize("month", [1, 3, 4, 5, 7, 8, 10, 11])
    def test_non_distribution_months(self, month):
        assert get_bonus_distribution_month(month) is False


# ──────────────────────────────────────────────────────────────
# 跨年度入職 3 個月資格
# ──────────────────────────────────────────────────────────────
class TestFestivalBonusEligibilityAcrossYear:
    """reference_date 與 hire_date 跨年的情境"""

    def test_hire_nov_15_eligible_in_feb_distribution(self):
        # 2025/11/15 入職，2026/2 發放節慶獎金 → 2026/2/15 滿 3 個月
        # 使用 2 月最後一天做 reference 應可領
        assert (
            is_eligible_for_festival_bonus(
                "2025-11-15", reference_date=date(2026, 2, 28)
            )
            is True
        )

    def test_hire_dec_1_not_yet_eligible_at_feb_1(self):
        # 2025/12/1 入職，2026/2/1 還沒滿 3 個月（差 1 天）
        assert (
            is_eligible_for_festival_bonus(
                "2025-12-01", reference_date=date(2026, 2, 28)
            )
            is False
        )

    def test_hire_dec_1_eligible_at_mar_1(self):
        # 2025/12/1 入職，2026/3/1 剛好滿 3 個月
        assert (
            is_eligible_for_festival_bonus(
                "2025-12-01", reference_date=date(2026, 3, 1)
            )
            is True
        )

    def test_hire_jan_15_eligible_jun_distribution(self):
        # 2026/1/15 入職 → 2026/4/15 滿 3 個月；6 月發放時必符合
        assert (
            is_eligible_for_festival_bonus(
                "2026-01-15", reference_date=date(2026, 6, 30)
            )
            is True
        )

    def test_none_hire_date_defaults_eligible(self):
        assert is_eligible_for_festival_bonus(None) is True

    def test_invalid_date_string_defaults_eligible(self):
        # 日期格式錯誤 → 預設可領（寬鬆策略，鎖定當前行為）
        assert is_eligible_for_festival_bonus("not-a-date") is True


# ──────────────────────────────────────────────────────────────
# 閏年 2 月折算
# ──────────────────────────────────────────────────────────────
class TestLeapYearFebruaryProration:
    """閏年 2 月天數 29，非閏年 28"""

    def test_leap_year_feb_mid_month_hire(self, engine):
        # 2024/2 是閏年（29 天）；2/15 入職 → 在職 15 天 / 29
        result = engine._prorate_base_salary(29000, "2024-02-15", 2024, 2)
        assert result == pytest.approx(29000 * 15 / 29)

    def test_non_leap_feb_mid_month_hire(self, engine):
        # 2026/2 非閏年（28 天）；2/15 入職 → 在職 14 天 / 28
        result = engine._prorate_base_salary(28000, "2026-02-15", 2026, 2)
        assert result == pytest.approx(28000 * 14 / 28)

    def test_leap_year_feb_29_hire(self, engine):
        # 閏年 2/29 入職 → 在職 1 天 / 29
        result = engine._prorate_base_salary(30000, "2024-02-29", 2024, 2)
        assert result == pytest.approx(30000 * 1 / 29)

    def test_leap_year_feb_resign_mid(self, engine):
        # 閏年 2/10 離職 → 在職 10 / 29
        result = engine._prorate_for_period(30000, None, "2024-02-10", 2024, 2)
        assert result == pytest.approx(30000 * 10 / 29)


# ──────────────────────────────────────────────────────────────
# 保費級距上下限
# ──────────────────────────────────────────────────────────────
class TestInsuranceBracketBounds:
    """跨級距邊界與上下限行為"""

    @pytest.fixture
    def service(self):
        return InsuranceService()

    def test_salary_below_min_bracket(self, service):
        # 薪資 1000 < 最低級距 1500
        result = service.calculate(salary=1000, dependents=0)
        assert result.insured_amount == 1500

    def test_salary_above_max_bracket(self, service):
        # 薪資 500000 超過最高級距 313000
        result = service.calculate(salary=500000, dependents=0)
        assert result.insured_amount == 313000

    def test_labor_fee_stays_at_cap_above_45800(self, service):
        # 勞保上限 45800：48200 的勞保費與 45800 相同
        at_cap = service.calculate(salary=45800, dependents=0)
        above = service.calculate(salary=48200, dependents=0)
        assert at_cap.labor_employee == above.labor_employee

    def test_pension_fee_stays_at_cap_above_150000(self, service):
        # 勞退雇主提撥上限 150000
        at_cap = service.calculate(salary=150000, dependents=0)
        above = service.calculate(salary=156400, dependents=0)
        assert at_cap.pension_employer == above.pension_employer

    def test_crossing_bracket_increases_total(self, service):
        # 跨級距一定會讓保費單調非遞減
        lower = service.calculate(salary=28800, dependents=0)
        higher = service.calculate(salary=31800, dependents=0)
        assert higher.total_employee >= lower.total_employee

    def test_zero_salary_falls_to_min_bracket(self, service):
        result = service.calculate(salary=0, dependents=0)
        assert result.insured_amount == 1500

    def test_negative_salary_rejected(self, service):
        with pytest.raises(ValueError):
            service.calculate(salary=-100, dependents=0)

    def test_pension_self_rate_out_of_range_rejected(self, service):
        with pytest.raises(ValueError):
            service.calculate(salary=30000, pension_self_rate=0.07)
        with pytest.raises(ValueError):
            service.calculate(salary=30000, pension_self_rate=-0.01)


# ──────────────────────────────────────────────────────────────
# 月份邊界：1 月 / 12 月
# ──────────────────────────────────────────────────────────────
class TestMonthEndEdgeDates:
    def test_jan_31_hire_one_day(self, engine):
        # 1/31 入職 → 在職 1 天 / 31
        result = engine._prorate_base_salary(31000, "2026-01-31", 2026, 1)
        assert result == pytest.approx(31000 * 1 / 31)

    def test_dec_31_hire_one_day(self, engine):
        # 12/31 入職 → 在職 1 天 / 31
        result = engine._prorate_base_salary(31000, "2025-12-31", 2025, 12)
        assert result == pytest.approx(31000 * 1 / 31)

    def test_apr_30_resign_last_day_no_proration(self, engine):
        # 月末離職（day == 月總天數）→ 全額，不折算（鎖定 line 92 條件 <）
        result = engine._prorate_for_period(30000, None, "2026-04-30", 2026, 4)
        assert result == 30000

    def test_hire_prior_year_no_proration(self, engine):
        # 去年入職 → 本月不折算
        result = engine._prorate_base_salary(30000, "2024-06-15", 2026, 4)
        assert result == 30000

    def test_resign_different_year_no_proration(self, engine):
        # 未來年離職日（非本月）→ 本月不折算
        result = engine._prorate_for_period(30000, None, "2027-01-15", 2026, 12)
        assert result == 30000


# ──────────────────────────────────────────────────────────────
# 整合：非發放月 calculate_salary 不計節慶獎金
# ──────────────────────────────────────────────────────────────
class TestCalculateSalaryNonDistributionMonth:
    def _emp(self):
        return {
            "employee_id": "E001",
            "name": "測試",
            "title": "幼兒園教師",
            "position": "幼兒園教師",
            "employee_type": "regular",
            "base_salary": 30000,
            "hourly_rate": 0,
            "insurance_salary": 30300,
            "dependents": 0,
            "hire_date": "2024-01-01",
        }

    @pytest.mark.parametrize("month", [1, 3, 4, 5, 7, 8, 10, 11])
    def test_non_distribution_month_zero_festival(self, engine, month):
        bd = engine.calculate_salary(self._emp(), year=2026, month=month)
        assert bd.festival_bonus == 0
        assert bd.overtime_bonus == 0
