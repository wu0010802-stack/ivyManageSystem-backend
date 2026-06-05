"""請假/遲到扣款無條件捨去（對齊義華薪資 Excel 慣例）回歸測試。

園所 Excel 對小數時數的請假/遲到扣款一律無條件捨去；系統原本 round_half_up，
導致小數時數請假每筆多扣 1 元（對員工不利）。改為 round_down 對齊。
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.rounding import round_down
from services.salary.utils import _sum_leave_deduction
from services.salary.deduction import calculate_attendance_deduction
from models.attendance import AttendanceStatus


def test_round_down_truncates():
    assert round_down(491.67) == 491
    assert round_down(245.83) == 245
    assert round_down(696.75) == 696
    assert round_down(2200.0) == 2200  # 整數不變
    assert round_down(619.33) == 619


def _partial(hours, leave_type, start):
    att = SimpleNamespace(status="present", partial_leave_hours=hours)
    lv = SimpleNamespace(leave_type=leave_type, deduction_ratio=None, start_date=start)
    return (att, lv)


def _fullday(leave_type, start):
    att = SimpleNamespace(status=AttendanceStatus.LEAVE.value, partial_leave_hours=None)
    lv = SimpleNamespace(leave_type=leave_type, deduction_ratio=None, start_date=start)
    return (att, lv)


def test_personal_half_day_floored():
    # 張庭滋 base 29500：事假 4h → 4/8 × (29500/30) × 1.0 = 491.67 → 491（非 492）
    from datetime import date

    daily = 29500 / 30
    total = _sum_leave_deduction([_partial(4, "personal", date(2026, 5, 5))], daily)
    assert total == 491


def test_sick_half_day_floored():
    # 王品嬑 base 29500：病假 4h → 4/8 × 983.33 × 0.5 = 245.83 → 245（非 246）
    from datetime import date

    daily = 29500 / 30
    total = _sum_leave_deduction([_partial(4, "sick", date(2026, 5, 5))], daily)
    assert total == 245


def test_personal_full_day_integer_unchanged():
    # 田甄宓 base 30000：事假 1 天 = 1000（整數，捨去不影響）
    from datetime import date

    daily = 30000 / 30
    total = _sum_leave_deduction([_fullday("personal", date(2026, 5, 5))], daily)
    assert total == 1000


def test_late_deduction_floored():
    # base 30000：遲到 10 分 → 10 × 30000/(30×8×60) = 20.83 → 20（非 21）
    att = SimpleNamespace(
        total_late_minutes=10,
        total_early_minutes=0,
        late_count=1,
        early_leave_count=0,
        missing_punch_in_count=0,
        missing_punch_out_count=0,
    )
    res = calculate_attendance_deduction(att, daily_salary=1000, base_salary=30000)
    assert res["late_deduction"] == 20
    assert res["early_leave_deduction"] == 0
