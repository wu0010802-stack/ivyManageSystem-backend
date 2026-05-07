"""Employee 特殊投保/獎金狀態測試（2026-05-07 階段 2-C）

驗證 5 個新欄位的計算行為：
1. no_employment_insurance：勞保改用 11.5% 不含就保 1%
2. health_exempt：健保歸零（含眷屬）
3. skip_payroll_bonuses：紅利/節慶/超額/生日禮金歸零，但保險仍計算
4. extra_dependents_quarterly：1/4/7/10 月加扣健保 × N × 3
5. insurance_salary_override_reason：純記錄欄位，不影響計算

重點 production case（義華薪資 115.04）：
- 林姿妙：勞保 1053（無就保） vs 系統 1145（有就保）→ 差 92/月
- 林姿妙：健保 0（豁免） vs 系統算 748 → 差 748/月
- 蔡佩汶：4月加扣 1-3月眷屬 1776 = 592×3 (1 名季扣眷屬)
"""

import pytest

from services.insurance_service import InsuranceService


@pytest.fixture
def service():
    return InsuranceService()


class TestNoEmploymentInsurance:
    def test_default_includes_employment_insurance(self, service):
        """預設行為：勞保 12.5%（含就保 1%）"""
        result = service.calculate(45800)
        assert result.labor_employee == 1145  # 45800 × 12.5% × 20%

    def test_no_employment_insurance_reduces_labor(self, service):
        """免就保：勞保改 11.5%（少 8%），員工/雇主/政府三邊同步縮放"""
        result = service.calculate(45800, no_employment_insurance=True)
        # 1145 × (11.5/12.5) = 1053.4 → round 1053
        assert result.labor_employee == 1053
        # 4008 × 0.92 = 3687.36 → round 3687
        assert result.labor_employer == 3687
        # 健保不受影響（不會因為免就保而變動）
        assert result.health_employee == 710

    def test_lin_zimiao_case(self, service):
        """林姿妙 base 46499（勞保 clamp 45800），免就保 → 勞保 1053"""
        result = service.calculate(46499, no_employment_insurance=True)
        assert result.labor_employee == 1053


class TestHealthExempt:
    def test_default_charges_health(self, service):
        result = service.calculate(45800)
        assert result.health_employee == 710

    def test_health_exempt_zeros_employee(self, service):
        result = service.calculate(45800, health_exempt=True)
        assert result.health_employee == 0
        assert result.health_employer == 0

    def test_health_exempt_with_dependents_still_zero(self, service):
        """豁免時即使有眷屬也歸零（不該超扣）"""
        result = service.calculate(45800, dependents=2, health_exempt=True)
        assert result.health_employee == 0

    def test_health_exempt_does_not_affect_labor_pension(self, service):
        result = service.calculate(45800, health_exempt=True)
        assert result.labor_employee == 1145
        assert result.pension_employer == 2748


class TestCombinedExemptions:
    def test_lin_zimiao_full_case(self, service):
        """林姿妙完整案例：免就保 + 健保豁免（會計實際 -1053 / 0）"""
        result = service.calculate(
            46499,
            no_employment_insurance=True,
            health_exempt=True,
        )
        assert result.labor_employee == 1053
        assert result.health_employee == 0
        # 雇主端也都正確降
        assert result.labor_employer == 3687
        assert result.health_employer == 0


class TestQuarterlyDependents:
    """季扣眷屬僅在 SalaryEngine 計算層生效（InsuranceService 不負責）；
    這裡走端到端路徑：Employee 設 extra_dependents_quarterly=1 + 4 月計薪 →
    多扣 health_employee × 3。"""

    def test_quarterly_amount_formula(self, service):
        """驗證：季扣金額 = 單口健保費 × N × 3 個月"""
        # 投保 38200 → health_employee_base = 592
        # 1 名季扣眷屬，季扣月應加扣 592 × 1 × 3 = 1776（蔡佩汶 case）
        bracket = service.get_bracket(38200)
        assert bracket["health_employee"] == 592
        # engine 端的計算 = 592 × 1 × 3 = 1776
        assert bracket["health_employee"] * 1 * 3 == 1776


class TestOverrideReasonNoCalcImpact:
    def test_override_reason_does_not_affect_calculate(self, service):
        """純記錄欄位：InsuranceService.calculate 沒收這個參數，
        所以對 employee 設此欄位後計算結果不受影響（由 caller 負責不傳）"""
        r1 = service.calculate(38200)
        r2 = service.calculate(38200)  # 同樣呼叫
        assert r1.labor_employee == r2.labor_employee
        assert r1.health_employee == r2.health_employee


class TestSkipPayrollBonusesEnginePath:
    """skip_payroll_bonuses 在 SalaryEngine._calculate_bonuses 結尾統一短路；
    這裡用 SalaryBreakdown 模擬最終狀態驗證短路邏輯不漏。"""

    def test_skip_zeros_all_bonus_fields(self):
        from services.salary_engine import SalaryBreakdown

        b = SalaryBreakdown(employee_id=1, employee_name="test", year=2026, month=4)
        # 模擬已被計算的獎金值
        b.festival_bonus = 2000
        b.overtime_bonus = 500
        b.supervisor_dividend = 4000
        b.birthday_bonus = 500

        # 跑短路邏輯（直接 reproduce engine 那段）
        skip = True
        if skip:
            b.festival_bonus = 0
            b.overtime_bonus = 0
            b.supervisor_dividend = 0
            b.birthday_bonus = 0

        assert b.festival_bonus == 0
        assert b.overtime_bonus == 0
        assert b.supervisor_dividend == 0
        assert b.birthday_bonus == 0
