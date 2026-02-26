"""InsuranceService 單元測試"""
import pytest
from services.insurance_service import InsuranceService, InsuranceCalculation


@pytest.fixture
def service():
    return InsuranceService()


class TestGetBracket:
    """測試級距查找"""

    def test_exact_match(self, service):
        """薪資剛好等於級距金額"""
        bracket = service.get_bracket(30300)
        assert bracket['amount'] == 30300

    def test_between_brackets(self, service):
        """薪資介於兩個級距之間，取較高級"""
        bracket = service.get_bracket(30500)
        assert bracket['amount'] == 31800

    def test_below_minimum(self, service):
        """薪資低於最低級距"""
        bracket = service.get_bracket(500)
        assert bracket['amount'] == 1500

    def test_above_maximum(self, service):
        """薪資超過最高級距，回傳最高級"""
        bracket = service.get_bracket(500000)
        assert bracket['amount'] == 313000

    def test_exact_minimum(self, service):
        """薪資等於最低級距"""
        bracket = service.get_bracket(1500)
        assert bracket['amount'] == 1500

    def test_exact_maximum(self, service):
        """薪資等於最高級距"""
        bracket = service.get_bracket(313000)
        assert bracket['amount'] == 313000

    def test_labor_cap_at_45800(self, service):
        """勞保投保上限 45800，超過後勞保費不再增加"""
        bracket_at_cap = service.get_bracket(45800)
        bracket_above = service.get_bracket(48200)
        assert bracket_at_cap['labor_employee'] == 1145
        assert bracket_above['labor_employee'] == 1145  # 勞保費相同

    def test_pension_cap_at_150000(self, service):
        """勞退提撥上限 150000"""
        bracket_at_cap = service.get_bracket(150000)
        bracket_above = service.get_bracket(156400)
        assert bracket_at_cap['pension'] == 9000
        assert bracket_above['pension'] == 9000  # 勞退費相同


class TestCalculate:
    """測試保費計算"""

    def test_no_dependents(self, service):
        """無眷屬"""
        result = service.calculate(salary=30000, dependents=0)
        assert isinstance(result, InsuranceCalculation)
        assert result.insured_amount == 30300
        assert result.labor_employee == 758
        assert result.health_employee == 470  # 本人
        assert result.pension_employee == 0  # 無自提
        assert result.total_employee == 758 + 470

    def test_with_dependents(self, service):
        """有 2 位眷屬，健保費倍增"""
        result = service.calculate(salary=30000, dependents=2)
        assert result.health_employee == 470 * 3  # 本人 + 2 眷屬

    def test_dependents_capped_at_3(self, service):
        """眷屬人數上限 3"""
        result_3 = service.calculate(salary=30000, dependents=3)
        result_5 = service.calculate(salary=30000, dependents=5)
        assert result_3.health_employee == result_5.health_employee  # 都是 4 倍

    def test_pension_self_contribution(self, service):
        """勞退自提 6%"""
        result = service.calculate(salary=30000, dependents=0, pension_self_rate=0.06)
        assert result.pension_employee == round(30000 * 0.06)
        assert result.total_employee == 758 + 470 + round(30000 * 0.06)

    def test_total_employer(self, service):
        """雇主負擔總額"""
        result = service.calculate(salary=30000, dependents=0)
        expected = result.labor_employer + result.health_employer + result.pension_employer
        assert result.total_employer == expected

    def test_salary_range_format(self, service):
        """salary_range 格式化"""
        result = service.calculate(salary=30000, dependents=0)
        assert result.salary_range == '30,300'

    def test_low_salary(self, service):
        """低薪資（部分工時）"""
        result = service.calculate(salary=1000, dependents=0)
        assert result.insured_amount == 1500

    def test_high_salary(self, service):
        """高薪資超出所有級距"""
        result = service.calculate(salary=400000, dependents=0)
        assert result.insured_amount == 313000
