"""基本工資合規驗證單元測試（勞基法第 21 條）

月薪制（regular）與時薪制（hourly）員工設定的底薪與時薪，
不得低於勞動部公告之法定基本工資。
"""

import pytest
from fastapi import HTTPException

from services.salary.minimum_wage import (
    validate_minimum_wage,
    MINIMUM_MONTHLY_WAGE,
    MINIMUM_HOURLY_WAGE,
)


class TestValidateMinimumWage:
    def test_regular_at_minimum_passes(self):
        """月薪等於基本工資，合規"""
        validate_minimum_wage("regular", MINIMUM_MONTHLY_WAGE, 0)

    def test_regular_above_minimum_passes(self):
        validate_minimum_wage("regular", MINIMUM_MONTHLY_WAGE + 100, 0)

    def test_regular_below_minimum_raises(self):
        """月薪低於基本工資 → 400"""
        with pytest.raises(HTTPException) as exc:
            validate_minimum_wage("regular", MINIMUM_MONTHLY_WAGE - 1, 0)
        assert exc.value.status_code == 400
        assert "基本工資" in exc.value.detail

    def test_regular_zero_base_is_allowed(self):
        """底薪為 0 表示尚未設定，不檢查（允許建立員工後再補）"""
        validate_minimum_wage("regular", 0, 0)

    def test_hourly_at_minimum_passes(self):
        validate_minimum_wage("hourly", 0, MINIMUM_HOURLY_WAGE)

    def test_hourly_below_minimum_raises(self):
        """時薪低於基本工資 → 400"""
        with pytest.raises(HTTPException) as exc:
            validate_minimum_wage("hourly", 0, MINIMUM_HOURLY_WAGE - 1)
        assert exc.value.status_code == 400
        assert "基本工資" in exc.value.detail

    def test_hourly_zero_rate_is_allowed(self):
        """時薪為 0 表示尚未設定，不檢查"""
        validate_minimum_wage("hourly", 0, 0)

    def test_minimum_wage_constants_meet_2026_statutory(self):
        """常數至少為勞動部 2026 年公告值（若政府調升，測試強制開發者更新）"""
        assert MINIMUM_MONTHLY_WAGE >= 29500
        assert MINIMUM_HOURLY_WAGE >= 196
