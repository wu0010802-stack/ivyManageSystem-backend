"""二代健保補充保費測試（兼職薪資 ≥ 門檻時扣 2.11%）

Excel 註記：「115.01 月起未達 29500 元，兼職所得無需扣除二代健保」
"""

import pytest

from services.salary.engine import SalaryEngine


@pytest.fixture
def engine():
    return SalaryEngine(load_from_db=False)


def _hourly_emp(hourly_rate, work_hours, *, insurance=0):
    return {
        "employee_id": "H001",
        "name": "才藝老師",
        "employee_type": "hourly",
        "hourly_rate": hourly_rate,
        "work_hours": work_hours,
        "base_salary": 0,
        "insurance_salary": insurance,
    }


class TestSupplementaryHealth:
    def test_below_threshold_no_deduction(self, engine):
        """月薪 < 29500 不扣補充保費（Excel 案例：歐瑞煌 13750）。"""
        emp = _hourly_emp(550, 25)  # 550×25 = 13750
        breakdown = engine.calculate_salary(emp, 2026, 4)
        assert breakdown.hourly_total == 13750
        assert breakdown.supplementary_health_employee == 0

    def test_at_threshold_deducted(self, engine):
        """月薪 = 29500 觸發扣款（含起扣值）。"""
        emp = _hourly_emp(295, 100)  # 295×100 = 29500
        breakdown = engine.calculate_salary(emp, 2026, 4)
        assert breakdown.hourly_total == 29500
        # 29500 × 0.0211 = 622.45 → round 622
        assert breakdown.supplementary_health_employee == 622

    def test_above_threshold_deducted(self, engine):
        """月薪 > 29500 扣款（Excel 案例：李麗珍 51760）。"""
        emp = _hourly_emp(220, 235)  # 220 × 235 = 51700
        breakdown = engine.calculate_salary(emp, 2026, 4)
        assert breakdown.hourly_total == 51700
        # 51700 × 0.0211 = 1090.87 → round 1091
        assert breakdown.supplementary_health_employee == 1091

    def test_regular_employee_not_charged(self, engine):
        """正職員工（regular）不扣補充保費（走勞健保正常路徑）。"""
        emp = {
            "employee_id": "E001",
            "name": "正職",
            "employee_type": "regular",
            "base_salary": 50000,
            "insurance_salary": 0,  # 不投保（簡化測試）
        }
        breakdown = engine.calculate_salary(emp, 2026, 4)
        assert breakdown.supplementary_health_employee == 0

    def test_supplementary_included_in_health_insurance(self, engine):
        """補充保費應併入 health_insurance（會計只看一筆健保扣款）。

        hourly 員工因 fallback 投保（hourly_rate × 176）也會扣基本健保；
        本測試驗證 supplementary 確實加上去，而非取代基本健保。
        """
        emp = _hourly_emp(550, 60)  # 33000

        # 算 baseline（門檻提高使 supplementary=0）
        engine.insurance_service.supplementary_health_threshold = 999999
        base = engine.calculate_salary(emp, 2026, 4)
        engine.insurance_service.supplementary_health_threshold = 29500

        # 啟用補充保費
        with_suppl = engine.calculate_salary(emp, 2026, 4)
        # 33000 × 0.0211 = 696.3 → 696
        assert with_suppl.supplementary_health_employee == 696
        # health_insurance 應比 baseline 多出 supplementary
        assert with_suppl.health_insurance == base.health_insurance + 696

    def test_rate_from_insurance_service(self, engine):
        """調整 InsuranceService.supplementary_health_rate 後該值生效。"""
        engine.insurance_service.supplementary_health_rate = 0.03
        engine.insurance_service.supplementary_health_threshold = 29500
        emp = _hourly_emp(550, 60)  # 33000
        breakdown = engine.calculate_salary(emp, 2026, 4)
        assert breakdown.supplementary_health_employee == 990  # 33000 × 0.03

    def test_threshold_from_insurance_service(self, engine):
        """調整門檻後生效。"""
        engine.insurance_service.supplementary_health_threshold = 50000
        emp = _hourly_emp(550, 60)  # 33000 < 50000 → 不扣
        breakdown = engine.calculate_salary(emp, 2026, 4)
        assert breakdown.supplementary_health_employee == 0
