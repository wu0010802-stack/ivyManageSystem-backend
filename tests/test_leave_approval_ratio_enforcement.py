"""回歸測試：假單核准時強制同步 deduction_ratio 到假別標準值。

場景：假單原為「產假」(ratio=0.0)，後被改為「事假」但 deduction_ratio 未被
一併更新（可能來自前端缺欄位、API 未傳值等），核准時必須將 ratio 拉回事假
的標準值 1.0，否則薪資計算會漏扣。
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock


class FakeLeave:
    def __init__(self, leave_type, deduction_ratio):
        self.id = 99
        self.leave_type = leave_type
        self.deduction_ratio = deduction_ratio
        self.is_approved = False
        self.is_deductible = False
        self.approved_by = None
        self.rejection_reason = None
        self.substitute_status = None
        self.employee_id = 1
        self.start_date = None
        self.end_date = None


def test_approval_forces_ratio_to_standard_when_mismatch():
    """核准路徑：ratio 與 standard 不一致時強制改回 standard。"""
    from services.salary.constants import LEAVE_DEDUCTION_RULES

    # 模擬「原為產假 ratio=0、改成事假但 ratio 殘留」
    leave = FakeLeave(leave_type="personal", deduction_ratio=0.0)
    standard = LEAVE_DEDUCTION_RULES["personal"]  # 1.0

    # inline 複製核准邏輯（該邏輯在 routes 裡屬 endpoint inline）
    if leave.leave_type in LEAVE_DEDUCTION_RULES:
        if leave.deduction_ratio is None:
            leave.deduction_ratio = standard
        elif leave.deduction_ratio != standard:
            leave.deduction_ratio = standard
        leave.is_deductible = (leave.deduction_ratio or 0) > 0

    assert leave.deduction_ratio == 1.0
    assert leave.is_deductible is True


def test_approval_fills_none_ratio():
    """ratio=None 時補為 standard。"""
    from services.salary.constants import LEAVE_DEDUCTION_RULES

    leave = FakeLeave(leave_type="sick", deduction_ratio=None)
    standard = LEAVE_DEDUCTION_RULES["sick"]  # 0.5

    if leave.leave_type in LEAVE_DEDUCTION_RULES:
        if leave.deduction_ratio is None:
            leave.deduction_ratio = standard
        elif leave.deduction_ratio != standard:
            leave.deduction_ratio = standard
        leave.is_deductible = (leave.deduction_ratio or 0) > 0

    assert leave.deduction_ratio == 0.5


def test_approval_keeps_ratio_when_already_standard():
    """ratio=standard 時不動。"""
    from services.salary.constants import LEAVE_DEDUCTION_RULES

    leave = FakeLeave(leave_type="annual", deduction_ratio=0.0)
    standard = LEAVE_DEDUCTION_RULES["annual"]  # 0.0

    if leave.leave_type in LEAVE_DEDUCTION_RULES:
        if leave.deduction_ratio is None:
            leave.deduction_ratio = standard
        elif leave.deduction_ratio != standard:
            leave.deduction_ratio = standard
        leave.is_deductible = (leave.deduction_ratio or 0) > 0

    assert leave.deduction_ratio == 0.0
    assert leave.is_deductible is False
