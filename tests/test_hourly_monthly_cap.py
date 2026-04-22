"""勞基法第 32 條第 2 項合規：每月延長工時上限 46 小時。

時薪員工單日工時分段計費（正常、1.34、1.67），但當月累計加班時數
（第 9 小時起）超過 46h 後，超過部分仍須發薪但按正常倍率（1.0），
不得加成。此測試鎖定 `_calc_daily_hourly_pay_with_cap` 之契約。
"""

import os
import sys
import math

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.salary.hourly import _calc_daily_hourly_pay_with_cap
from services.salary.constants import (
    HOURLY_OT1_RATE,
    HOURLY_OT2_RATE,
    HOURLY_REGULAR_HOURS,
    HOURLY_OT1_CAP_HOURS,
)

RATE = 200.0


class TestDailyHourlyPayWithCap:
    def test_no_overtime_no_cap_effect(self):
        """日工時 8h，quota 無影響"""
        pay, ot = _calc_daily_hourly_pay_with_cap(8.0, RATE, remaining_ot_quota=0.0)
        assert math.isclose(pay, RATE * 8)
        assert ot == 0.0

    def test_overtime_within_quota(self):
        """日工時 10h，剩餘 quota 10h → 2h 都在 1.34 倍率內"""
        pay, ot = _calc_daily_hourly_pay_with_cap(10.0, RATE, remaining_ot_quota=10.0)
        expected = RATE * 8 + RATE * 2 * HOURLY_OT1_RATE
        assert math.isclose(pay, expected)
        assert ot == 2.0

    def test_overtime_exceeds_quota_falls_back_to_regular(self):
        """日工時 10h，剩餘 quota 1h → 1h 享 1.34 倍、1h 退回 1.0 倍"""
        pay, ot = _calc_daily_hourly_pay_with_cap(10.0, RATE, remaining_ot_quota=1.0)
        # 8h 正常 + 1h × 1.34 + 1h × 1.0
        expected = RATE * 8 + RATE * 1 * HOURLY_OT1_RATE + RATE * 1 * 1.0
        assert math.isclose(pay, expected)
        assert ot == 1.0  # 只消耗 1h quota

    def test_overtime_fully_over_quota(self):
        """剩餘 quota 0h → 所有加班都以 1.0 倍率計"""
        pay, ot = _calc_daily_hourly_pay_with_cap(11.0, RATE, remaining_ot_quota=0.0)
        # 8h 正常 + 3h × 1.0
        expected = RATE * 8 + RATE * 3 * 1.0
        assert math.isclose(pay, expected)
        assert ot == 0.0

    def test_11_hours_quota_enough_for_ot1_only(self):
        """11h 工時，quota 2h → 2h 享 1.34、1h 退回 1.0（ot2 不加成）"""
        pay, ot = _calc_daily_hourly_pay_with_cap(11.0, RATE, remaining_ot_quota=2.0)
        expected = RATE * 8 + RATE * 2 * HOURLY_OT1_RATE + RATE * 1 * 1.0
        assert math.isclose(pay, expected)
        assert ot == 2.0

    def test_infinite_quota_matches_no_cap(self):
        """quota=inf → 行為與無上限版本一致"""
        pay, ot = _calc_daily_hourly_pay_with_cap(
            11.0, RATE, remaining_ot_quota=float("inf")
        )
        expected = RATE * 8 + RATE * 2 * HOURLY_OT1_RATE + RATE * 1 * HOURLY_OT2_RATE
        assert math.isclose(pay, expected)
        assert ot == 3.0

    def test_exactly_at_quota_boundary(self):
        """剛好 ot 用完所有 quota"""
        pay, ot = _calc_daily_hourly_pay_with_cap(10.0, RATE, remaining_ot_quota=2.0)
        expected = RATE * 8 + RATE * 2 * HOURLY_OT1_RATE
        assert math.isclose(pay, expected)
        assert ot == 2.0


class TestMonthlyAccumulation:
    """多日累計超過 46h 的情境"""

    def test_backward_compat_old_signature(self):
        """舊版 `_calc_daily_hourly_pay(hours, rate)` 仍可呼叫"""
        from services.salary.hourly import _calc_daily_hourly_pay

        result = _calc_daily_hourly_pay(10.0, RATE)
        expected = RATE * 8 + RATE * 2 * HOURLY_OT1_RATE
        assert math.isclose(result, expected)

    def test_caller_tracks_quota_across_days(self):
        """模擬 engine 呼叫端：每日累計 ot_used，剩餘 quota 遞減。"""
        MAX = 46.0
        ot_used = 0.0
        total_pay = 0.0
        # 12 天、每天 12 小時 → 每天 4 小時 OT → 總 OT 48 小時（超過 46）
        for _ in range(12):
            remaining = max(0.0, MAX - ot_used)
            pay, used = _calc_daily_hourly_pay_with_cap(
                12.0, RATE, remaining_ot_quota=remaining
            )
            ot_used += used
            total_pay += pay
        assert ot_used == pytest.approx(46.0)
        # 前 11 天全享加成（共 44h OT）+ 第 12 天 2h 享加成、2h 退回 1.0
        # 前 11 天：RATE * (8 + 2*1.34 + 2*1.67) × 11
        # 第 12 天：RATE * (8 + 2*1.34 + 2*1.0)  （ot2 退回正常）
        day_full = RATE * (8 + 2 * HOURLY_OT1_RATE + 2 * HOURLY_OT2_RATE)
        day_capped = RATE * (8 + 2 * HOURLY_OT1_RATE + 2 * 1.0)
        assert math.isclose(total_pay, day_full * 11 + day_capped)
