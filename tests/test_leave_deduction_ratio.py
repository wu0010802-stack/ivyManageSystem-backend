"""
回歸測試：請假扣款應讀取 LeaveRecord.deduction_ratio，不應硬寫假別比例。

Bug 背景：salary_engine.py 忽略 LeaveRecord.deduction_ratio 欄位，
改用模組內硬寫的 LEAVE_DEDUCTION_RULES，導致 HR 設定的覆蓋值形同虛設。
"""
import sys
import os
import pytest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.salary_engine import _sum_leave_deduction


def _make_leave(leave_type: str, leave_hours: float, deduction_ratio=None):
    """建立最小假單物件（不碰 DB）"""
    return SimpleNamespace(
        leave_type=leave_type,
        leave_hours=leave_hours,
        deduction_ratio=deduction_ratio,
    )


class TestSumLeaveDeduction:
    """_sum_leave_deduction() 純函式的單元測試"""

    # daily_salary = 30000 / 30 = 1000
    DAILY_SALARY = 1000.0

    def test_full_pay_sick_leave_with_zero_ratio(self):
        """病假 deduction_ratio=0.0 → 全薪特殊病假，扣款應為 0"""
        leaves = [_make_leave("sick", leave_hours=8, deduction_ratio=0.0)]
        result = _sum_leave_deduction(leaves, self.DAILY_SALARY)
        assert result == 0

    def test_half_deduction_with_explicit_ratio(self):
        """deduction_ratio=0.5 → 扣半薪（8h = 1天 = 500元）"""
        leaves = [_make_leave("sick", leave_hours=8, deduction_ratio=0.5)]
        result = _sum_leave_deduction(leaves, self.DAILY_SALARY)
        assert result == 500

    def test_full_deduction_personal_leave(self):
        """事假 deduction_ratio=1.0 → 全扣（8h = 1天 = 1000元）"""
        leaves = [_make_leave("personal", leave_hours=8, deduction_ratio=1.0)]
        result = _sum_leave_deduction(leaves, self.DAILY_SALARY)
        assert result == 1000

    def test_fallback_to_leave_rules_when_ratio_is_none(self):
        """deduction_ratio=None → fallback 到 LEAVE_DEDUCTION_RULES[leave_type]

        sick 的預設比例為 0.5，所以 8h × 1000 × 0.5 = 500
        """
        leaves = [_make_leave("sick", leave_hours=8, deduction_ratio=None)]
        result = _sum_leave_deduction(leaves, self.DAILY_SALARY)
        assert result == 500

    def test_fallback_annual_leave_no_deduction(self):
        """deduction_ratio=None，假別為特休 → fallback 到 0.0，扣款應為 0"""
        leaves = [_make_leave("annual", leave_hours=8, deduction_ratio=None)]
        result = _sum_leave_deduction(leaves, self.DAILY_SALARY)
        assert result == 0

    def test_multiple_leaves_sum_correctly(self):
        """多筆假單加總：病假半天（deduction_ratio=0.5）+ 事假一天（deduction_ratio=1.0）"""
        leaves = [
            _make_leave("sick", leave_hours=4, deduction_ratio=0.5),    # 4/8 × 1000 × 0.5 = 250
            _make_leave("personal", leave_hours=8, deduction_ratio=1.0), # 8/8 × 1000 × 1.0 = 1000
        ]
        result = _sum_leave_deduction(leaves, self.DAILY_SALARY)
        assert result == 1250

    def test_unknown_leave_type_fallback_to_full_deduction(self):
        """deduction_ratio=None 且假別不在 LEAVE_DEDUCTION_RULES → fallback 預設全扣（1.0）"""
        leaves = [_make_leave("unknown_type", leave_hours=8, deduction_ratio=None)]
        result = _sum_leave_deduction(leaves, self.DAILY_SALARY)
        assert result == 1000

    def test_empty_leaves_returns_zero(self):
        """無假單 → 扣款為 0"""
        result = _sum_leave_deduction([], self.DAILY_SALARY)
        assert result == 0
