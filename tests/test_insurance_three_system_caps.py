"""勞保 / 健保 / 勞退三制度各自最高級距（2026 年）

- 勞保（含就保）：月投保薪資最高 NT$45,800
- 健保：月投保金額最高 NT$219,500
- 勞退：月提繳工資最高 NT$150,000

高薪員工薪資超過各制度上限時，應以各自上限計算。
系統若用單一級距算三制度，健保與勞退會多收（bug）。
"""

import pytest

from services.insurance_service import (
    InsuranceService,
    LABOR_MAX_INSURED_SALARY,
    HEALTH_MAX_INSURED_SALARY,
    PENSION_MAX_INSURED_SALARY,
)


@pytest.fixture
def service():
    return InsuranceService()


class TestThreeSystemCaps:
    def test_max_insured_constants(self):
        assert LABOR_MAX_INSURED_SALARY == 45800
        assert HEALTH_MAX_INSURED_SALARY == 219500
        assert PENSION_MAX_INSURED_SALARY == 150000

    def test_low_salary_below_all_caps_unaffected(self, service):
        """月薪 38,200 低於三制度上限 — 行為不變"""
        result = service.calculate(38200)
        assert result.labor_employee == 955
        assert result.health_employee == 592
        assert result.pension_employer == 2292

    def test_200k_salary_caps_labor_to_45800(self, service):
        """月薪 200,000 → 勞保應以 45,800 計（員工 1145）而非 200,000"""
        result = service.calculate(200000)
        # 45,800 級的 labor_employee = 1145
        assert result.labor_employee == 1145
        assert result.labor_employer == 4008

    def test_200k_salary_caps_pension_to_150000(self, service):
        """月薪 200,000 → 勞退應以 150,000 計（雇主 6% = 9,000）"""
        result = service.calculate(200000)
        assert result.pension_employer == 9000

    def test_200k_salary_health_uses_actual_level(self, service):
        """月薪 200,000 健保仍按實際級距（< 219,500 上限）"""
        result = service.calculate(200000)
        # 200,000 在 219,500 以內，健保不應被 clamp 到低於 200,000 級的值
        assert result.health_employee > 0
        # 而且不該是 45,800 對應的 710（避免用錯上限）
        assert result.health_employee > 710

    def test_300k_salary_caps_health_to_219500(self, service):
        """月薪 300,000 → 健保應以 219,500 計（不超過上限）"""
        result = service.calculate(300000)
        # 219,500 級的 health_employee = 3404（2026 表）
        assert result.health_employee == 3404
        assert result.health_employer == 10622

    def test_300k_salary_caps_all_three(self, service):
        """月薪 300,000 → 三個制度全部觸及上限"""
        result = service.calculate(300000)
        assert result.labor_employee == 1145  # 45,800 級
        assert result.labor_employer == 4008
        assert result.health_employee == 3404  # 219,500 級
        assert result.pension_employer == 9000  # 150,000 級

    def test_pension_self_respects_cap(self, service):
        """員工勞退自提 6% 也應以 150,000 為上限，不是 300,000 × 6% = 18,000"""
        result = service.calculate(300000, pension_self_rate=0.06)
        assert result.pension_employee == 9000  # 150,000 × 6%

    def test_labor_government_share_respects_cap(self, service):
        """勞保政府負擔 10% 也以 45,800 為基底"""
        result = service.calculate(300000)
        # 45800 × 12.5% × 10% = 572.5 ≈ 573 (or 572, 依 round)
        expected = round(LABOR_MAX_INSURED_SALARY * 0.125 * 0.10)
        assert result.labor_government == expected
