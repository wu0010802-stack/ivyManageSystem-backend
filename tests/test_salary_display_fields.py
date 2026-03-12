import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api.salary_fields import calculate_display_bonus_total, calculate_total_allowances


class TestCalculateDisplayBonusTotal:
    def test_sums_bonus_fields_without_double_counting_bonus_amount(self):
        """festival/overtime 不可再被 bonus_amount 重複加總。"""
        record = SimpleNamespace(
            festival_bonus=1200,
            overtime_bonus=800,
            performance_bonus=500,
            special_bonus=300,
            bonus_amount=3500,
            supervisor_dividend=1500,
        )

        result = calculate_display_bonus_total(record)

        assert result == 2800

    def test_treats_missing_values_as_zero(self):
        """None 欄位應視為 0，避免 API 顯示 NaN 或 TypeError。"""
        record = SimpleNamespace(
            festival_bonus=None,
            overtime_bonus=None,
            performance_bonus=None,
            special_bonus=None,
        )

        result = calculate_display_bonus_total(record)

        assert result == 0


class TestCalculateTotalAllowances:
    def test_sums_all_allowance_fields(self):
        record = SimpleNamespace(
            supervisor_allowance=1000,
            teacher_allowance=2000,
            meal_allowance=2400,
            transportation_allowance=600,
            other_allowance=300,
        )

        result = calculate_total_allowances(record)

        assert result == 6300
