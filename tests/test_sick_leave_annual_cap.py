"""勞基法第 43 條 + 勞工請假規則第 4 條合規：
病假一年內累計未逾 30 日（240h）者工資折半發給，超過部分雇主得不給薪。

回歸測試：`_sum_leave_deduction` 必須接受年度已用病假時數參數，
超過 240h 的部分 ratio 視為 1.0（扣全薪）而非 0.5。
"""

import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.salary.utils import _sum_leave_deduction
from services.salary.constants import SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS


def _mk(leave_type: str, hours: float, ratio=None, start: date = date(2026, 4, 1)):
    return SimpleNamespace(
        leave_type=leave_type,
        leave_hours=hours,
        deduction_ratio=ratio,
        start_date=start,
    )


DAILY = 1000.0  # 月薪 30000 / 30


class TestSickLeaveAnnualCap:
    def test_cap_constant_is_240_hours(self):
        """cap 常數應為 240h（30 天 × 8 小時）"""
        assert SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS == 240.0

    def test_sick_within_cap_is_half_pay(self):
        """年度尚未達 240h，病假扣半薪"""
        result = _sum_leave_deduction(
            [_mk("sick", 8)], DAILY, ytd_sick_hours_before_month=0.0
        )
        assert result == 500  # 8h × 1000 × 0.5

    def test_sick_fully_over_cap_is_full_deduction(self):
        """本月之前已用 240h，本月病假全數扣全薪"""
        result = _sum_leave_deduction(
            [_mk("sick", 8)], DAILY, ytd_sick_hours_before_month=240.0
        )
        assert result == 1000  # 8h × 1000 × 1.0

    def test_sick_partially_over_cap_splits(self):
        """本月之前已用 232h，本月請 16h：8h 半薪 + 8h 全薪"""
        result = _sum_leave_deduction(
            [_mk("sick", 16)], DAILY, ytd_sick_hours_before_month=232.0
        )
        # 前 8h 半薪：500；後 8h 全薪：1000；合計 1500
        assert result == 1500

    def test_multiple_sick_leaves_in_month_sorted_by_date(self):
        """本月多筆病假，按 start_date 由早到晚累計；最後一筆跨越上限"""
        leaves = [
            _mk("sick", 16, start=date(2026, 4, 20)),  # 後請
            _mk("sick", 232, start=date(2026, 4, 1)),  # 先請（極端測試值）
        ]
        # YTD before = 0；先算 232h 全半薪（232×125×0.5=14500）
        # 再算 16h：前 8h 半薪（500）+ 後 8h 全薪（1000）
        result = _sum_leave_deduction(leaves, DAILY, ytd_sick_hours_before_month=0.0)
        expected = (
            (232 / 8) * DAILY * 0.5 + (8 / 8) * DAILY * 0.5 + (8 / 8) * DAILY * 1.0
        )
        assert result == expected

    def test_manual_ratio_override_respected_but_counted(self):
        """真正偏離標準（0.5）的人工覆寫才優先使用，且時數仍計入年度累計"""
        # 病假 8h 被 HR 標為全薪（ratio=0）— 尊重
        leaves = [
            _mk("sick", 8, ratio=0.0, start=date(2026, 4, 1)),
            _mk("sick", 8, start=date(2026, 4, 2)),  # 無覆寫，走自動計算
        ]
        # ytd=232：第一筆 0×... = 0（尊重覆寫），累計到 240；第二筆 8h 全部全薪 = 1000
        result = _sum_leave_deduction(leaves, DAILY, ytd_sick_hours_before_month=232.0)
        assert result == 1000

    def test_standard_ratio_value_not_treated_as_override(self):
        """假單以 ratio=0.5（標準值）核准時仍應套用年度上限。

        關鍵：api/leaves.py 核准流程會把 deduction_ratio 強制設為 standard
        （病假 = 0.5），此時 `_sum_leave_deduction` 必須把它視為「未覆寫」，
        以便年度超過 240h 時仍能以全扣（1.0）計算。若把 0.5 直接套用，
        會導致勞基法 30 日上限形同虛設。
        """
        leaves = [_mk("sick", 8, ratio=0.5, start=date(2026, 4, 1))]
        result = _sum_leave_deduction(leaves, DAILY, ytd_sick_hours_before_month=240.0)
        assert result == 1000  # ratio=0.5 不是真正的人工覆寫，應被上限接管

    def test_non_sick_leave_unaffected(self):
        """事假、特休等非病假不受上限影響"""
        result = _sum_leave_deduction(
            [_mk("personal", 8), _mk("annual", 8)],
            DAILY,
            ytd_sick_hours_before_month=300.0,  # 即使 ytd 爆表
        )
        assert result == 1000  # 8h 事假全扣 + 8h 特休不扣

    def test_backward_compat_no_ytd_arg_defaults_to_zero(self):
        """舊呼叫端沒傳 ytd 參數時預設 0，行為與改前相同"""
        result = _sum_leave_deduction([_mk("sick", 8)], DAILY)
        assert result == 500  # 半薪

    def test_explicit_ratio_none_at_boundary(self):
        """deduction_ratio=None 且剛好在上限臨界點：240→240 不觸發全扣"""
        result = _sum_leave_deduction(
            [_mk("sick", 0)], DAILY, ytd_sick_hours_before_month=240.0
        )
        assert result == 0
