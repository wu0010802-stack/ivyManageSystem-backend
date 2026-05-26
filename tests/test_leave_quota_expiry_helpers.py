"""Pure function helpers for leave quota expiry — date/hourly wage resolvers."""

from datetime import date
from unittest.mock import MagicMock

import pytest

from services.leave_quota_expiry.helpers import (
    _next_month,
    _add_one_year_with_feb29_handling,
    _resolve_hourly_wage,
)


class TestNextMonth:
    """跨年 12→1 wrap"""

    def test_next_month_normal(self):
        assert _next_month(date(2026, 4, 15)) == (2026, 5)

    def test_next_month_year_wrap(self):
        assert _next_month(date(2026, 12, 31)) == (2027, 1)


class TestAddOneYearWithFeb29Handling:
    """2/29 + 1y 落非閏年順延 2/28"""

    def test_add_one_year_normal(self):
        assert _add_one_year_with_feb29_handling(date(2025, 4, 1)) == date(2026, 4, 1)

    def test_add_one_year_feb29_to_non_leap(self):
        # 2024 是閏年，2025 不是 → 2/29 → 2/28
        assert _add_one_year_with_feb29_handling(date(2024, 2, 29)) == date(2025, 2, 28)


class TestResolveHourlyWage:
    """月薪/30/8 或 hourly_rate"""

    def test_resolve_hourly_wage_hourly_employee(self):
        emp = MagicMock(employee_type="hourly", hourly_rate=200.0)
        assert _resolve_hourly_wage(emp, date(2026, 4, 1)) == 200.0

    def test_resolve_hourly_wage_monthly_employee(self):
        emp = MagicMock(employee_type="monthly", base_salary=48000.0)
        # 48000 / 30 / 8 = 200
        assert _resolve_hourly_wage(emp, date(2026, 4, 1)) == 200.0
