"""
回歸測試：時薪制員工日工時超過 8 小時應依勞基法第 24 條分段計費。

Bug 背景：salary_engine.py 計算時薪制員工薪資時，所有工時一律以正常倍率計算，
未套用第 9–10 小時 1.34 倍、第 11 小時起 1.67 倍的加班費規定，造成欠薪。
"""
import sys
import os
import math
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.salary_engine import (
    _calc_daily_hourly_pay,
    HOURLY_OT1_RATE,
    HOURLY_OT2_RATE,
    HOURLY_REGULAR_HOURS,
    HOURLY_OT1_CAP_HOURS,
)

RATE = 200.0  # 測試用時薪（易於計算）


class TestCalcDailyHourlyPay:
    """_calc_daily_hourly_pay() 純函式的單元測試"""

    def test_exactly_8_hours_no_overtime(self):
        """8h 正常工時，無加班 → rate × 8"""
        result = _calc_daily_hourly_pay(8.0, RATE)
        assert math.isclose(result, RATE * 8, rel_tol=1e-9)

    def test_9_hours_first_ot_tier(self):
        """9h → 正常 8h + 第9小時 1.34倍"""
        expected = RATE * 8 + RATE * 1 * HOURLY_OT1_RATE
        result = _calc_daily_hourly_pay(9.0, RATE)
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_10_hours_first_ot_tier_full(self):
        """10h → 正常 8h + 第9-10小時 2h × 1.34倍"""
        expected = RATE * 8 + RATE * 2 * HOURLY_OT1_RATE
        result = _calc_daily_hourly_pay(10.0, RATE)
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_11_hours_second_ot_tier(self):
        """11h → 正常 8h + 第9-10小時 2h × 1.34倍 + 第11小時 1h × 1.67倍"""
        expected = RATE * 8 + RATE * 2 * HOURLY_OT1_RATE + RATE * 1 * HOURLY_OT2_RATE
        result = _calc_daily_hourly_pay(11.0, RATE)
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_less_than_8_hours_all_regular(self):
        """7h 未滿 8 小時 → 全程正常倍率 rate × 7，無加成"""
        result = _calc_daily_hourly_pay(7.0, RATE)
        assert math.isclose(result, RATE * 7, rel_tol=1e-9)

    def test_zero_hours(self):
        """0h 邊界 → 0"""
        result = _calc_daily_hourly_pay(0.0, RATE)
        assert result == 0.0

    def test_9h_pay_greater_than_simple_multiplication(self):
        """9h 加班計費後應多於單純 rate × 9（加班有加成，不是等比）"""
        tiered = _calc_daily_hourly_pay(9.0, RATE)
        simple = RATE * 9
        assert tiered > simple, f"加班後薪資 {tiered} 應大於等比計算 {simple}"

    def test_partial_hours_in_ot1(self):
        """9.5h → 正常 8h + OT1 段（8h-9.5h = 1.5h）× 1.34倍"""
        # 超時從第 8 小時後開始：9.5 - 8 = 1.5h 屬第一加班分段
        expected = RATE * 8 + RATE * 1.5 * HOURLY_OT1_RATE
        result = _calc_daily_hourly_pay(9.5, RATE)
        assert math.isclose(result, expected, rel_tol=1e-9)

    def test_12_hours_max_cap_scenario(self):
        """12h（上限）→ 正常 8h + 2h × 1.34倍 + 2h × 1.67倍"""
        expected = RATE * 8 + RATE * 2 * HOURLY_OT1_RATE + RATE * 2 * HOURLY_OT2_RATE
        result = _calc_daily_hourly_pay(12.0, RATE)
        assert math.isclose(result, expected, rel_tol=1e-9)
