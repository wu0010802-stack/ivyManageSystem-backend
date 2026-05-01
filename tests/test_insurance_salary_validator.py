"""投保薪資合規守衛（勞保條例第 14 條 / 勞退條例第 14 條）

「投保薪資不得低於實際工資」。
月薪制：insurance_salary_level 非 0 時 ≥ base_salary
時薪制：insurance_salary_level 非 0 時 ≥ hourly_rate × 估算月工時（176h）
任何員工：非 0 時不得低於基本工資
"""

import pytest
from fastapi import HTTPException

from services.salary.insurance_salary import (
    validate_insurance_salary,
    resolve_insurance_salary_raw,
    ESTIMATED_MONTHLY_HOURS,
)
from services.salary.minimum_wage import MINIMUM_MONTHLY_WAGE


class TestValidateInsuranceSalary:
    def test_zero_is_allowed_fallback_to_base(self):
        """insurance=0 → 系統 fallback 至 base_salary，允許"""
        validate_insurance_salary(
            employee_type="regular",
            base_salary=37160,
            insurance_salary_level=0,
            hourly_rate=0,
        )

    def test_none_is_allowed(self):
        validate_insurance_salary(
            employee_type="regular",
            base_salary=37160,
            insurance_salary_level=None,
            hourly_rate=0,
        )

    def test_regular_insurance_equal_base_passes(self):
        validate_insurance_salary(
            employee_type="regular",
            base_salary=37160,
            insurance_salary_level=37160,
            hourly_rate=0,
        )

    def test_regular_insurance_higher_than_base_passes(self):
        """投保高於實際（高報，對員工有利）— 允許"""
        validate_insurance_salary(
            employee_type="regular",
            base_salary=37160,
            insurance_salary_level=38200,
            hourly_rate=0,
        )

    def test_regular_insurance_below_base_raises(self):
        """呂宜凡案例：月薪 37160 但投保 33000 → 違反第 14 條"""
        with pytest.raises(HTTPException) as exc:
            validate_insurance_salary(
                employee_type="regular",
                base_salary=37160,
                insurance_salary_level=33000,
                hourly_rate=0,
            )
        assert exc.value.status_code == 400
        detail = exc.value.detail
        assert detail["code"] == "INSURANCE_BELOW_BASE"
        assert detail["context"]["kind"] == "below_monthly_wage"
        assert detail["context"]["base"] == 37160.0
        assert detail["context"]["current"] == 33000.0
        assert detail["context"]["suggested"] == 37160.0

    def test_any_insurance_below_minimum_wage_raises(self):
        """投保薪資 < 基本工資 → 拒絕（極端 data entry 錯誤，如陳益超 5000）"""
        with pytest.raises(HTTPException) as exc:
            validate_insurance_salary(
                employee_type="regular",
                base_salary=30000,
                insurance_salary_level=5000,
                hourly_rate=0,
            )
        assert exc.value.status_code == 400

    def test_hourly_insurance_below_estimated_monthly_raises(self):
        """時薪 200 × 176 = 35200，投保只有 30000 → 低報"""
        with pytest.raises(HTTPException) as exc:
            validate_insurance_salary(
                employee_type="hourly",
                base_salary=0,
                insurance_salary_level=30000,
                hourly_rate=200,
            )
        assert exc.value.status_code == 400

    def test_hourly_insurance_above_estimated_passes(self):
        validate_insurance_salary(
            employee_type="hourly",
            base_salary=0,
            insurance_salary_level=36000,
            hourly_rate=200,
        )

    def test_hourly_without_rate_allows_zero_insurance(self):
        """hourly_rate=0（尚未設定時薪）且 insurance=0 → 允許"""
        validate_insurance_salary(
            employee_type="hourly",
            base_salary=0,
            insurance_salary_level=0,
            hourly_rate=0,
        )

    def test_estimated_monthly_hours_constant(self):
        """估算月工時為 176h（22 工作日 × 8h）"""
        assert ESTIMATED_MONTHLY_HOURS == 176


class TestResolveInsuranceSalaryRaw:
    """決定查級距的投保薪資 raw 值：
    insurance_salary_level > 0 優先；否則 regular 用 base_salary，hourly 用 hourly_rate × 176
    """

    def test_explicit_insurance_takes_priority(self):
        assert resolve_insurance_salary_raw("regular", 37160, 38200, 0) == 38200

    def test_regular_falls_back_to_base_salary(self):
        """月薪制未設 insurance → 用 base_salary"""
        assert resolve_insurance_salary_raw("regular", 37160, 0, 0) == 37160
        assert resolve_insurance_salary_raw("regular", 37160, None, 0) == 37160

    def test_hourly_falls_back_to_rate_times_176(self):
        """時薪制未設 insurance → 用 hourly_rate × 176（之前會跳過保費）"""
        assert resolve_insurance_salary_raw("hourly", 0, 0, 200) == 200 * 176

    def test_hourly_with_both_zero_returns_zero(self):
        """時薪制連 hourly_rate 都 0 → 回傳 0（呼叫端需處理）"""
        assert resolve_insurance_salary_raw("hourly", 0, 0, 0) == 0.0

    def test_regular_all_zero_returns_zero(self):
        """月薪制 base=0 且 insurance=0 → 回傳 0"""
        assert resolve_insurance_salary_raw("regular", 0, 0, 0) == 0.0

    def test_explicit_insurance_overrides_hourly_fallback(self):
        """時薪制若已設 insurance，優先用它，不套用 176 公式（前提 ≥ 估算）"""
        assert resolve_insurance_salary_raw("hourly", 0, 36000, 200) == 36000

    def test_regular_short_report_auto_corrected_to_base(self):
        """呂宜凡案例：insurance=33000 < base=37160 → 薪資計算用 max=37160 防短報"""
        assert resolve_insurance_salary_raw("regular", 37160, 33000, 0) == 37160

    def test_hourly_short_report_auto_corrected_to_estimated(self):
        """時薪 200 × 176 = 35200 > insurance=30000 → 用 35200 防短報"""
        assert resolve_insurance_salary_raw("hourly", 0, 30000, 200) == 35200

    def test_high_reported_insurance_preserved(self):
        """高報（對員工有利）：insurance=38200 > base=30000 → 保留 38200"""
        assert resolve_insurance_salary_raw("regular", 30000, 38200, 0) == 38200
