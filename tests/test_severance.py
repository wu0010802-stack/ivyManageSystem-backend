"""資遣費與平均工資計算單元測試（勞基法第 2、17 條；勞退條例第 12 條）

平均工資（第 2 條第 4 款）：事由發生當日前 6 個月內所得工資總額 ÷ 該期間總日數 × 30
舊制資遣費（第 17 條）：15 年內每滿 1 年發給 1 個月，超過 15 年每年 0.5 個月，上限 45 個月
新制資遣費（勞退條例第 12 條）：每滿 1 年發給 0.5 個月，上限 6 個月
"""

import math
from datetime import date

import pytest

from services.salary.severance import (
    calculate_service_years,
    calculate_average_monthly_wage,
    calculate_severance_pay_new,
    calculate_severance_pay_old,
)


class TestCalculateServiceYears:
    def test_one_year_exact(self):
        """到職 2024-01-01 → 離職 2025-01-01：1 年"""
        years = calculate_service_years(date(2024, 1, 1), date(2025, 1, 1))
        assert math.isclose(years, 1.0, abs_tol=0.01)

    def test_half_year(self):
        """約半年"""
        years = calculate_service_years(date(2024, 1, 1), date(2024, 7, 1))
        assert 0.49 < years < 0.51

    def test_end_before_hire_returns_zero(self):
        """離職日在到職日之前 → 0"""
        assert calculate_service_years(date(2025, 1, 1), date(2024, 1, 1)) == 0.0

    def test_same_day_returns_near_zero(self):
        """同日到職離職 → 接近 0"""
        assert calculate_service_years(date(2025, 1, 1), date(2025, 1, 1)) < 0.01


class TestCalculateAverageMonthlyWage:
    def test_six_months_same_wage(self):
        """6 個月每月 30000，各月 30 天 → 月平均工資 30000"""
        records = [(30000, 30)] * 6
        assert math.isclose(calculate_average_monthly_wage(records), 30000.0)

    def test_empty_returns_zero(self):
        assert calculate_average_monthly_wage([]) == 0.0

    def test_zero_days_returns_zero(self):
        """避免除零"""
        assert calculate_average_monthly_wage([(30000, 0)]) == 0.0

    def test_weighted_by_days(self):
        """不同月日數混合"""
        # (30000+30000+30000+31000+31000+31000) / (30+30+30+31+31+31) × 30
        records = [(30000, 30)] * 3 + [(31000, 31)] * 3
        total_wage = 30000 * 3 + 31000 * 3
        total_days = 30 * 3 + 31 * 3
        expected = total_wage / total_days * 30
        assert math.isclose(calculate_average_monthly_wage(records), expected)


class TestSeverancePayNew:
    """新制（勞退條例第 12 條）：年資 × 0.5 個月，上限 6 個月"""

    def test_zero_years(self):
        assert calculate_severance_pay_new(0.0, 30000) == 0.0

    def test_one_year(self):
        """1 年 × 0.5 × 30000 = 15000"""
        assert calculate_severance_pay_new(1.0, 30000) == 15000.0

    def test_ten_years_hits_cap(self):
        """10 年 × 0.5 = 5 個月 < 6，未達上限"""
        assert calculate_severance_pay_new(10.0, 30000) == 150000.0

    def test_twelve_years_at_cap(self):
        """12 年 × 0.5 = 6 個月，剛好上限"""
        assert calculate_severance_pay_new(12.0, 30000) == 180000.0

    def test_twenty_years_capped_at_six_months(self):
        """20 年仍以 6 個月為上限"""
        assert calculate_severance_pay_new(20.0, 30000) == 180000.0


class TestSeverancePayOld:
    """舊制（勞基法第 17 條）：每滿 1 年發給 1 個月平均工資，剩餘月數按比例，條文無上限"""

    def test_zero_years(self):
        assert calculate_severance_pay_old(0.0, 30000) == 0.0

    def test_one_year(self):
        """1 年 × 1 × 30000 = 30000"""
        assert calculate_severance_pay_old(1.0, 30000) == 30000.0

    def test_fifteen_years(self):
        """滿 15 年 × 30000 = 450000"""
        assert calculate_severance_pay_old(15.0, 30000) == 450000.0

    def test_twenty_years_no_halving(self):
        """第 17 條無 15 年後折半規定：20 × 30000 = 600000"""
        assert calculate_severance_pay_old(20.0, 30000) == 600000.0

    def test_no_statutory_cap(self):
        """第 17 條無法定上限：50 年 × 30000 = 1,500,000"""
        assert calculate_severance_pay_old(50.0, 30000) == 50 * 30000
