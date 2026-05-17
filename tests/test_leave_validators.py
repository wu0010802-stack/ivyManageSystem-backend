"""tests/test_leave_validators.py — utils/leave_validators.py 純函式測試。"""

from datetime import date

import pytest

from utils.leave_validators import (
    validate_leave_date_order,
    validate_leave_hours_value,
)


class TestValidateLeaveHoursValue:
    def test_minimum_half_hour(self):
        assert validate_leave_hours_value(0.5) == 0.5

    def test_integer_hours(self):
        assert validate_leave_hours_value(8) == 8

    def test_half_hour_multiples(self):
        assert validate_leave_hours_value(1.5) == 1.5
        assert validate_leave_hours_value(3.5) == 3.5

    def test_maximum_480_hours(self):
        assert validate_leave_hours_value(480) == 480

    def test_too_small_raises(self):
        with pytest.raises(ValueError, match="至少 0.5"):
            validate_leave_hours_value(0.25)

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="至少 0.5"):
            validate_leave_hours_value(0)

    def test_too_large_raises(self):
        with pytest.raises(ValueError, match="不得超過 480"):
            validate_leave_hours_value(481)

    def test_not_half_hour_multiple_raises(self):
        with pytest.raises(ValueError, match="0.5 小時的倍數"):
            validate_leave_hours_value(1.3)

    def test_quarter_hour_raises(self):
        with pytest.raises(ValueError, match="0.5 小時的倍數"):
            validate_leave_hours_value(1.25)


class TestValidateLeaveDateOrder:
    def test_same_day_ok(self):
        # 不 raise 即 pass
        validate_leave_date_order(date(2026, 5, 17), date(2026, 5, 17))

    def test_normal_range_same_month(self):
        validate_leave_date_order(date(2026, 5, 1), date(2026, 5, 17))

    def test_end_before_start_raises(self):
        with pytest.raises(ValueError, match="結束日期不得早於開始日期"):
            validate_leave_date_order(date(2026, 5, 17), date(2026, 5, 10))

    def test_cross_month_raises(self):
        with pytest.raises(ValueError, match="不可跨月"):
            validate_leave_date_order(date(2026, 5, 30), date(2026, 6, 2))

    def test_cross_year_raises(self):
        with pytest.raises(ValueError, match="不可跨月"):
            validate_leave_date_order(date(2026, 12, 30), date(2027, 1, 2))

    def test_none_start_skips(self):
        # 其中一個為 None 時，跨月與順序檢查都應跳過
        validate_leave_date_order(None, date(2026, 5, 17))

    def test_none_end_skips(self):
        validate_leave_date_order(date(2026, 5, 17), None)

    def test_both_none_skips(self):
        validate_leave_date_order(None, None)

    def test_last_day_to_first_of_next_month_raises(self):
        with pytest.raises(ValueError, match="不可跨月"):
            validate_leave_date_order(date(2026, 5, 31), date(2026, 6, 1))
