"""tests/test_overtime_pay_calculator.py — overtime_pay_calculator 測試。"""

import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.overtime_pay_calculator import calculate_overtime_pay
from utils.rounding import round_half_up
from utils.constants import (
    DAILY_WORK_HOURS,
    HOLIDAY_RATE,
    MAX_OVERTIME_HOURS,
    RESTDAY_AFTER_8H_RATE,
    RESTDAY_FIRST_2H_RATE,
    RESTDAY_FIRST_SEGMENT,
    RESTDAY_MID_RATE,
    RESTDAY_SECOND_SEGMENT,
    WEEKDAY_AFTER_2H_RATE,
    WEEKDAY_FIRST_2H_RATE,
    WEEKDAY_THRESHOLD_HOURS,
)

MONTHLY_BASE_DAYS = 30
BASE = 30_000  # → 時薪 = 30000/30/8 = 125


def _hourly(base: float) -> float:
    return base / MONTHLY_BASE_DAYS / DAILY_WORK_HOURS


class TestCalculateOvertimePayValidation:
    def test_base_salary_zero_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            calculate_overtime_pay(0, 2, "weekday")
        assert exc.value.status_code == 400

    def test_base_salary_negative_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            calculate_overtime_pay(-100, 2, "weekday")
        assert exc.value.status_code == 400

    def test_base_salary_none_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            calculate_overtime_pay(None, 2, "weekday")  # type: ignore[arg-type]
        assert exc.value.status_code == 400

    def test_zero_hours_returns_zero(self):
        assert calculate_overtime_pay(BASE, 0, "weekday") == 0.0

    def test_negative_hours_returns_zero(self):
        assert calculate_overtime_pay(BASE, -1, "weekday") == 0.0


class TestCalculateOvertimePayWeekday:
    def test_within_threshold_uses_first_rate(self):
        # 2h × 125 × 1.34 = 335
        expected = round_half_up(
            _hourly(BASE) * WEEKDAY_THRESHOLD_HOURS * WEEKDAY_FIRST_2H_RATE
        )
        assert calculate_overtime_pay(BASE, 2, "weekday") == expected

    def test_over_threshold_splits_rates(self):
        # 4h: 2h × 1.34 + 2h × 1.67
        h = _hourly(BASE)
        expected = round_half_up(
            h * WEEKDAY_THRESHOLD_HOURS * WEEKDAY_FIRST_2H_RATE
            + h * (4 - WEEKDAY_THRESHOLD_HOURS) * WEEKDAY_AFTER_2H_RATE
        )
        assert calculate_overtime_pay(BASE, 4, "weekday") == expected

    def test_capped_at_max_overtime_hours(self):
        # 即使輸入 100h，應只計算 MAX_OVERTIME_HOURS
        result_huge = calculate_overtime_pay(BASE, 100, "weekday")
        result_max = calculate_overtime_pay(BASE, MAX_OVERTIME_HOURS, "weekday")
        assert result_huge == result_max

    def test_one_hour_within_threshold(self):
        expected = round_half_up(_hourly(BASE) * 1 * WEEKDAY_FIRST_2H_RATE)
        assert calculate_overtime_pay(BASE, 1, "weekday") == expected


class TestCalculateOvertimePayWeekend:
    def test_under_minimum_billed_as_min_hours(self):
        # 1h 應該被計為 2h
        h = _hourly(BASE)
        expected = round_half_up(h * RESTDAY_FIRST_SEGMENT * RESTDAY_FIRST_2H_RATE)
        assert calculate_overtime_pay(BASE, 1, "weekend") == expected
        # 0.5h 也是同樣（但 hours <=0 return 0；0.5 > 0 走計算）
        assert calculate_overtime_pay(BASE, 0.5, "weekend") == expected

    def test_within_first_segment(self):
        # 2h × 1.34
        h = _hourly(BASE)
        expected = round_half_up(h * 2 * RESTDAY_FIRST_2H_RATE)
        assert calculate_overtime_pay(BASE, 2, "weekend") == expected

    def test_in_second_segment(self):
        # 5h: 2h × 1.34 + 3h × 1.67
        h = _hourly(BASE)
        expected = round_half_up(
            h * RESTDAY_FIRST_SEGMENT * RESTDAY_FIRST_2H_RATE
            + h * (5 - RESTDAY_FIRST_SEGMENT) * RESTDAY_MID_RATE
        )
        assert calculate_overtime_pay(BASE, 5, "weekend") == expected

    def test_over_8_hours_uses_after_8h_rate(self):
        # 10h: 2h × 1.34 + 6h × 1.67 + 2h × 2.67
        h = _hourly(BASE)
        expected = round_half_up(
            h * RESTDAY_FIRST_SEGMENT * RESTDAY_FIRST_2H_RATE
            + h * (RESTDAY_SECOND_SEGMENT - RESTDAY_FIRST_SEGMENT) * RESTDAY_MID_RATE
            + h * (10 - RESTDAY_SECOND_SEGMENT) * RESTDAY_AFTER_8H_RATE
        )
        assert calculate_overtime_pay(BASE, 10, "weekend") == expected


class TestCalculateOvertimePayHoliday:
    def test_holiday_uses_double_rate(self):
        # 8h × 125 × 2 = 2000
        expected = round_half_up(_hourly(BASE) * 8 * HOLIDAY_RATE)
        assert calculate_overtime_pay(BASE, 8, "holiday") == expected

    def test_unknown_type_falls_through_to_holiday(self):
        # function 以 else 分支接所有非 weekday/weekend：例假 / 國定 / 自訂 string
        expected = round_half_up(_hourly(BASE) * 3 * HOLIDAY_RATE)
        assert calculate_overtime_pay(BASE, 3, "公假") == expected

    def test_holiday_respects_max_overtime_cap(self):
        capped = calculate_overtime_pay(BASE, 100, "holiday")
        normal = calculate_overtime_pay(BASE, MAX_OVERTIME_HOURS, "holiday")
        assert capped == normal
