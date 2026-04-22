"""回歸測試：SalaryManualAdjustRequest 應拒絕超出合理上限的金額輸入。

場景：管理員誤輸入 3000000（3 百萬）打成 30000000（3 千萬），應在 Pydantic
層就被拒絕，避免進入薪資計算後造成異常帳目。
"""

import sys
import os

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.salary import SalaryManualAdjustRequest, _MANUAL_ADJUST_FIELD_MAX


def test_field_at_upper_bound_accepted():
    """剛好到上限（10,000,000）應被接受"""
    req = SalaryManualAdjustRequest(base_salary=_MANUAL_ADJUST_FIELD_MAX)
    assert req.base_salary == _MANUAL_ADJUST_FIELD_MAX


def test_field_exceeds_upper_bound_rejected():
    """超過上限應拋 ValidationError"""
    with pytest.raises(ValidationError):
        SalaryManualAdjustRequest(base_salary=_MANUAL_ADJUST_FIELD_MAX + 1)


def test_negative_field_rejected():
    """負值仍如既有 ge=0 守衛拒絕"""
    with pytest.raises(ValidationError):
        SalaryManualAdjustRequest(festival_bonus=-1)


def test_normal_values_accepted():
    """正常金額不受影響"""
    req = SalaryManualAdjustRequest(
        base_salary=35000, festival_bonus=3000, overtime_pay=2000
    )
    assert req.base_salary == 35000
    assert req.festival_bonus == 3000
    assert req.overtime_pay == 2000


def test_all_fields_have_upper_bound():
    """所有欄位都應定義上限（守護新增欄位時忘了加 le）"""
    for field_name, field_info in SalaryManualAdjustRequest.model_fields.items():
        # 用 metadata 取上限 constraint
        constraints = getattr(field_info, "metadata", []) or []
        has_le = any(getattr(c, "le", None) is not None for c in constraints) or any(
            hasattr(c, "le") and c.le is not None for c in constraints
        )
        assert has_le, f"欄位 {field_name} 缺少 le 上限"
