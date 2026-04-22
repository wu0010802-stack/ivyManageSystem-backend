"""未休特休折算工資（勞基法第 38 條第 4 項）

「勞工之特別休假，因年度終結或契約終止而未休之日數，雇主應發給工資」。
折算公式：未休時數 × 時薪。時薪由呼叫端根據員工類型決定
（月薪制：月薪 ÷ 30 ÷ 8；時薪制：直接用 hourly_rate）。
"""

import math

import pytest

from services.salary.unused_leave_pay import (
    calculate_unused_annual_leave_hours,
    calculate_unused_leave_compensation,
)


class TestCalculateUnusedAnnualLeaveHours:
    def test_partial_usage(self):
        """應得 120h，已用 40h → 未休 80h"""
        assert calculate_unused_annual_leave_hours(120.0, 40.0) == 80.0

    def test_zero_usage(self):
        assert calculate_unused_annual_leave_hours(56.0, 0.0) == 56.0

    def test_fully_used(self):
        assert calculate_unused_annual_leave_hours(56.0, 56.0) == 0.0

    def test_over_used_clamps_to_zero(self):
        """已使用 > 應得（不應發生但防呆）→ 0，不得為負"""
        assert calculate_unused_annual_leave_hours(56.0, 70.0) == 0.0

    def test_none_inputs_treated_as_zero(self):
        assert calculate_unused_annual_leave_hours(None, None) == 0.0
        assert calculate_unused_annual_leave_hours(None, 10.0) == 0.0


class TestCalculateUnusedLeaveCompensation:
    """函式簽名：calculate_unused_leave_compensation(unused_hours, hourly_wage)"""

    def test_zero_hours(self):
        assert calculate_unused_leave_compensation(0.0, 125) == 0.0

    def test_zero_wage(self):
        assert calculate_unused_leave_compensation(40.0, 0) == 0.0

    def test_monthly_employee_equivalent(self):
        """月薪 30000 → 時薪 125；8h × 125 = 1000（即 1 日薪）"""
        result = calculate_unused_leave_compensation(8.0, 125.0)
        assert math.isclose(result, 1000.0)

    def test_hourly_employee_direct_rate(self):
        """時薪制員工：直接用 hourly_rate=200，未休 8h → 1600（不再回傳 0）"""
        result = calculate_unused_leave_compensation(8.0, 200.0)
        assert math.isclose(result, 1600.0)

    def test_full_annual_quota_monthly(self):
        """月薪 30000 時薪 125、未休 120h → 120 × 125 = 15000"""
        result = calculate_unused_leave_compensation(120.0, 125.0)
        assert math.isclose(result, 15000.0)

    def test_negative_hours_returns_zero(self):
        assert calculate_unused_leave_compensation(-1.0, 125) == 0.0

    def test_none_wage_returns_zero(self):
        assert calculate_unused_leave_compensation(8.0, None) == 0.0
