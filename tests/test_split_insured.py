"""勞保 / 健保 / 勞退 分項投保測試（2026-05-07 議題 B）

驗證 InsuranceService.calculate 接受 labor_insured / health_insured /
pension_insured kwargs；None=沿用 salary。

Production cases（義華薪資 115.04）：
- 王品嬑：勞保 29500 / 健保 30300 / 勞退 29500
- 林姿妙：勞保 46499→clamp 45800 / 健保豁免 / 勞退 45800
"""

import pytest

from services.insurance_service import InsuranceService


@pytest.fixture
def service():
    return InsuranceService()


class TestSplitInsured:
    def test_default_none_uses_salary(self, service):
        """三個 kwargs 皆 None → 行為與舊版單一 salary 相同"""
        r = service.calculate(38200, dependents=0)
        assert r.labor_employee == 955
        assert r.health_employee == 592
        assert r.pension_employer == 2292

    def test_labor_split_only(self, service):
        """only labor_insured 不同 → 勞保用獨立金額；健保/勞退仍用 salary"""
        # 王品嬑 case：salary=30300 (health), labor=29500
        r = service.calculate(30300, dependents=0, labor_insured=29500)
        # 29500 級距 → labor_employee 738
        assert r.labor_employee == 738
        # 30300 級距 → health 470
        assert r.health_employee == 470
        # pension 沿用 salary=30300 → 1818
        assert r.pension_employer == 1818

    def test_pension_split_only(self, service):
        """林姿妙 case：salary=46499 但勞退提繳工資對齊勞保上限 45800"""
        r = service.calculate(46499, pension_insured=45800)
        # pension 用 45800 級距 → 2748
        assert r.pension_employer == 2748
        # 勞保仍 clamp 45800 → 1145
        assert r.labor_employee == 1145

    def test_three_way_split(self, service):
        """三個都不同：勞保 29500 / 健保 30300 / 勞退 28590"""
        r = service.calculate(
            30300,
            labor_insured=29500,
            health_insured=30300,
            pension_insured=28590,
        )
        assert r.labor_employee == 738
        assert r.health_employee == 470
        # 28590 級距 → pension 1715
        assert r.pension_employer == 1715

    def test_split_respects_caps(self, service):
        """高薪 + 分項 → 各自 clamp 不爆"""
        r = service.calculate(
            500000,
            labor_insured=300000,  # > 勞保上限 45800
            pension_insured=200000,  # > 勞退上限 150000
        )
        assert r.labor_employee == 1145  # clamp 到 45800 級距
        assert r.pension_employer == 9000  # clamp 到 150000 級距

    def test_split_combined_with_no_employment_insurance(self, service):
        """分項 + no_employment_insurance：勞保用 labor_insured 級距 + 11.5% 縮放"""
        r = service.calculate(38200, labor_insured=29500, no_employment_insurance=True)
        # 29500 → labor_employee 738，× (11.5/12.5) = 678.96 → 679
        assert r.labor_employee == 679

    def test_split_combined_with_health_exempt(self, service):
        """分項 + health_exempt：health 仍歸零（無視 health_insured）"""
        r = service.calculate(46499, health_insured=46499, health_exempt=True)
        assert r.health_employee == 0

    def test_negative_split_raises(self, service):
        with pytest.raises(ValueError):
            service.calculate(30000, labor_insured=-1)

    def test_zero_split_treated_as_zero(self, service):
        """0 視為「不投保」（級距表第一筆 1500 級距 → 277/458/90）"""
        r = service.calculate(30000, labor_insured=0)
        # 0 → bracket lookup 取第一個 ≥0 級距 = 1500
        assert r.labor_employee == 277


class TestInsuredAmountField:
    def test_insured_amount_uses_main_salary_not_split(self, service):
        """response.insured_amount 仍代表 salary 級距（顯示用），不被 split 影響"""
        r = service.calculate(38200, labor_insured=29500)
        assert r.insured_amount == 38200
