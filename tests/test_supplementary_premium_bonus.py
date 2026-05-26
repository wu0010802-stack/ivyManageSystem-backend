"""二代健保補充保費 — 獎金路徑（健保法 §31 第 1 項第 1 款）。

覆蓋：
1. 公式正確性（per-payment incremental，basis = max(ytd_before, threshold)）
2. 跨月累進（第一次破門檻 vs 累計已破門檻）
3. 跨年度歸零
4. 多獎金來源加總（festival + appraisal_year_end）
5. 投保薪資跨月變動（threshold 用當月值）
6. supervisor_dividend 列入累計（業主分類）
7. birthday_bonus / overtime_pay 不入累計
8. 無投保 / 零獎金邊界
9. 與既有 hourly 路徑並存（apply_bonus_supplementary_to_breakdown 整合）
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from models.salary import SalaryRecord
from services.salary.breakdown import SalaryBreakdown
from services.salary.supplementary_premium import (
    apply_bonus_supplementary_to_breakdown,
    calculate_bonus_supplementary_fee,
    query_ytd_bonus_before,
)

EMP_ID = 1001


def _add_salary_record(session, *, year, month, **bonus_fields):
    """建一筆只填獎金欄位的 SalaryRecord。"""
    rec = SalaryRecord(employee_id=EMP_ID, salary_year=year, salary_month=month)
    for k, v in bonus_fields.items():
        setattr(rec, k, v)
    session.add(rec)
    session.flush()
    return rec


@pytest.fixture
def _no_appraisal_bonus():
    """預設 mock：appraisal_year_end_bonus query 回 0（非 2 月情境）。"""
    with patch(
        "services.salary.supplementary_premium.query_appraisal_year_end_bonus",
        return_value=0,
    ) as m:
        yield m


class TestQueryYtdBonusBefore:
    def test_empty_history_returns_zero(self, test_db_session):
        assert query_ytd_bonus_before(test_db_session, EMP_ID, 2026, 6) == 0

    def test_sums_all_bonus_fields(self, test_db_session):
        _add_salary_record(
            test_db_session,
            year=2026,
            month=2,
            festival_bonus=10000,
            overtime_bonus=2000,
            performance_bonus=3000,
            special_bonus=1000,
            supervisor_dividend=500,
            appraisal_year_end_bonus=20000,
        )
        # 不含 birthday_bonus / overtime_pay / base_salary
        rec = test_db_session.query(SalaryRecord).first()
        rec.birthday_bonus = 500
        rec.overtime_pay = 8000
        rec.base_salary = 30000
        test_db_session.flush()

        ytd = query_ytd_bonus_before(test_db_session, EMP_ID, 2026, 6)
        assert ytd == 36500  # 10000+2000+3000+1000+500+20000

    def test_excludes_current_and_future_months(self, test_db_session):
        _add_salary_record(test_db_session, year=2026, month=1, festival_bonus=5000)
        _add_salary_record(test_db_session, year=2026, month=6, festival_bonus=10000)
        _add_salary_record(test_db_session, year=2026, month=9, festival_bonus=20000)
        # 計算 6 月時應只含 1 月
        assert query_ytd_bonus_before(test_db_session, EMP_ID, 2026, 6) == 5000

    def test_excludes_other_years(self, test_db_session):
        _add_salary_record(test_db_session, year=2025, month=12, festival_bonus=99999)
        _add_salary_record(test_db_session, year=2026, month=1, festival_bonus=3000)
        assert query_ytd_bonus_before(test_db_session, EMP_ID, 2026, 3) == 3000


class TestCalculateBonusSupplementaryFee:
    def test_below_threshold_no_fee(self, test_db_session, _no_appraisal_bonus):
        # ytd_after=80000, threshold=4×30000=120000 → excess=0
        fee = calculate_bonus_supplementary_fee(
            test_db_session,
            EMP_ID,
            2026,
            6,
            breakdown_bonus_total=80000,
            health_insured_salary=30000,
        )
        assert fee == 0

    def test_at_threshold_no_fee(self, test_db_session, _no_appraisal_bonus):
        # ytd_after=120000, threshold=120000 → excess=0
        fee = calculate_bonus_supplementary_fee(
            test_db_session,
            EMP_ID,
            2026,
            6,
            breakdown_bonus_total=120000,
            health_insured_salary=30000,
        )
        assert fee == 0

    def test_first_breach_charges_only_excess(
        self, test_db_session, _no_appraisal_bonus
    ):
        # threshold=120000, prior=100000, this_month=80000 → ytd_after=180000
        # basis = max(100000, 120000) = 120000 → excess=60000 → 60000×0.0211=1266
        _add_salary_record(test_db_session, year=2026, month=2, festival_bonus=100000)
        fee = calculate_bonus_supplementary_fee(
            test_db_session,
            EMP_ID,
            2026,
            6,
            breakdown_bonus_total=80000,
            health_insured_salary=30000,
        )
        assert fee == 1266

    def test_subsequent_payment_full_amount(self, test_db_session, _no_appraisal_bonus):
        # prior 已破門檻：prior=180000 > threshold=120000，本月 40000 全額扣
        # basis = max(180000, 120000) = 180000 → excess=40000 → 844
        _add_salary_record(test_db_session, year=2026, month=6, festival_bonus=180000)
        fee = calculate_bonus_supplementary_fee(
            test_db_session,
            EMP_ID,
            2026,
            9,
            breakdown_bonus_total=40000,
            health_insured_salary=30000,
        )
        assert fee == 844  # 40000 × 0.0211 = 844

    def test_year_resets(self, test_db_session, _no_appraisal_bonus):
        # 前一年累計 999999 不計入本年
        _add_salary_record(test_db_session, year=2025, month=12, festival_bonus=999999)
        # 本年 ytd_before=0，threshold=120000，this_month=80000 → excess=0
        fee = calculate_bonus_supplementary_fee(
            test_db_session,
            EMP_ID,
            2026,
            6,
            breakdown_bonus_total=80000,
            health_insured_salary=30000,
        )
        assert fee == 0

    def test_threshold_uses_current_month_insured_salary(
        self, test_db_session, _no_appraisal_bonus
    ):
        # 同一 prior_ytd=100K + this_month=80K=180K
        # 6 月投保 30K → threshold=120K → excess=60K → 1266
        # 9 月投保 36.3K → threshold=145.2K → excess=34.8K → 734
        _add_salary_record(test_db_session, year=2026, month=2, festival_bonus=100000)
        fee_june = calculate_bonus_supplementary_fee(
            test_db_session,
            EMP_ID,
            2026,
            6,
            breakdown_bonus_total=80000,
            health_insured_salary=30000,
        )
        fee_sept = calculate_bonus_supplementary_fee(
            test_db_session,
            EMP_ID,
            2026,
            9,
            breakdown_bonus_total=80000,
            health_insured_salary=36300,
        )
        assert fee_june == 1266
        assert fee_sept == round(34800 * 0.0211)  # 734

    def test_supervisor_dividend_counted(self, test_db_session, _no_appraisal_bonus):
        # 業主分類：supervisor_dividend 列入累計
        _add_salary_record(
            test_db_session, year=2026, month=3, supervisor_dividend=120000
        )
        # ytd_before=120000, this_month=10000, threshold=120000
        # basis = max(120000, 120000) = 120000 → excess=10000 → 211
        fee = calculate_bonus_supplementary_fee(
            test_db_session,
            EMP_ID,
            2026,
            4,
            breakdown_bonus_total=10000,
            health_insured_salary=30000,
        )
        assert fee == 211

    def test_no_insured_salary_skips(self, test_db_session, _no_appraisal_bonus):
        fee = calculate_bonus_supplementary_fee(
            test_db_session,
            EMP_ID,
            2026,
            6,
            breakdown_bonus_total=500000,
            health_insured_salary=0,
        )
        assert fee == 0

    def test_no_current_bonus_skips(self, test_db_session, _no_appraisal_bonus):
        # 即便 prior 已破門檻，當月無獎金不扣
        _add_salary_record(test_db_session, year=2026, month=6, festival_bonus=500000)
        fee = calculate_bonus_supplementary_fee(
            test_db_session,
            EMP_ID,
            2026,
            9,
            breakdown_bonus_total=0,
            health_insured_salary=30000,
        )
        assert fee == 0

    def test_appraisal_year_end_bonus_counted_in_february(self, test_db_session):
        # 2 月情境：appraisal_year_end_bonus 進入當月累計
        # ytd_before=0, breakdown_bonus_total=80000, appraisal=60000 → ytd_after=140000
        # threshold=120000 → excess=20000 → 422
        with patch(
            "services.salary.supplementary_premium.query_appraisal_year_end_bonus",
            return_value=60000,
        ):
            fee = calculate_bonus_supplementary_fee(
                test_db_session,
                EMP_ID,
                2026,
                2,
                breakdown_bonus_total=80000,
                health_insured_salary=30000,
            )
        assert fee == 422


class TestApplyBonusSupplementaryToBreakdown:
    """整合測試：apply 真的 mutate breakdown 四欄位且不破壞 hourly 路徑既有值。"""

    class _FakeInsuranceService:
        supplementary_health_rate = 0.0211

        def get_bracket(self, raw):
            # 簡化：直接回 raw（test fixture 已給 bracket 值）
            return {"amount": raw}

    def _make_breakdown(self, **overrides):
        bd = SalaryBreakdown(
            employee_name="Test",
            employee_id="E001",
            year=2026,
            month=6,
            base_salary=30000,
            gross_salary=120000,  # 含獎金後的 gross
            health_insurance=458,  # 既有保費
            total_deduction=2000,
        )
        for k, v in overrides.items():
            setattr(bd, k, v)
        return bd

    def test_no_bonus_no_change(self, test_db_session, _no_appraisal_bonus):
        bd = self._make_breakdown()
        bd_before = (
            bd.health_insurance,
            bd.total_deduction,
            bd.supplementary_health_employee,
        )
        emp_dict = {
            "employee_type": "regular",
            "base_salary": 30000,
            "insurance_salary": 30000,
            "health_insured_salary": None,
        }
        fee = apply_bonus_supplementary_to_breakdown(
            test_db_session,
            emp_dict,
            bd,
            2026,
            6,
            self._FakeInsuranceService(),
            EMP_ID,
        )
        assert fee == 0
        assert (
            bd.health_insurance,
            bd.total_deduction,
            bd.supplementary_health_employee,
        ) == bd_before

    def test_first_breach_mutates_four_fields(
        self, test_db_session, _no_appraisal_bonus
    ):
        _add_salary_record(test_db_session, year=2026, month=2, festival_bonus=100000)
        bd = self._make_breakdown(festival_bonus=80000)
        emp_dict = {
            "employee_type": "regular",
            "base_salary": 30000,
            "insurance_salary": 30000,
            "health_insured_salary": None,
        }
        fee = apply_bonus_supplementary_to_breakdown(
            test_db_session,
            emp_dict,
            bd,
            2026,
            6,
            self._FakeInsuranceService(),
            EMP_ID,
        )
        assert fee == 1266
        assert bd.health_insurance == 458 + 1266
        assert bd.supplementary_health_employee == 1266
        assert bd.total_deduction == 2000 + 1266
        assert bd.net_salary == bd.gross_salary - bd.total_deduction

    def test_explicit_health_insured_salary_overrides_emp_dict_insurance(
        self, test_db_session, _no_appraisal_bonus
    ):
        # health_insured_salary 設 45800 覆寫 insurance_salary 30000
        _add_salary_record(test_db_session, year=2026, month=2, festival_bonus=100000)
        bd = self._make_breakdown(festival_bonus=120000)
        emp_dict = {
            "employee_type": "regular",
            "base_salary": 30000,
            "insurance_salary": 30000,
            "health_insured_salary": 45800,
        }
        # threshold = 4 × 45800 = 183200
        # ytd_after = 100000 + 120000 = 220000
        # basis = max(100000, 183200) = 183200
        # excess = 36800 → 36800 × 0.0211 = 776.48 → 776
        fee = apply_bonus_supplementary_to_breakdown(
            test_db_session,
            emp_dict,
            bd,
            2026,
            6,
            self._FakeInsuranceService(),
            EMP_ID,
        )
        assert fee == 776

    def test_preserves_existing_supplementary_health_from_hourly_path(
        self, test_db_session, _no_appraisal_bonus
    ):
        # 模擬 hourly 路徑已設了 supplementary_health_employee=622
        # 加上獎金路徑應 += 不是 overwrite
        _add_salary_record(test_db_session, year=2026, month=2, festival_bonus=100000)
        bd = self._make_breakdown(
            festival_bonus=80000,
            supplementary_health_employee=622,
        )
        emp_dict = {
            "employee_type": "regular",
            "base_salary": 30000,
            "insurance_salary": 30000,
            "health_insured_salary": None,
        }
        apply_bonus_supplementary_to_breakdown(
            test_db_session,
            emp_dict,
            bd,
            2026,
            6,
            self._FakeInsuranceService(),
            EMP_ID,
        )
        # 兩條路徑疊加
        assert bd.supplementary_health_employee == 622 + 1266


class TestCallerWiring:
    """Spy-patch test 驗三個 caller (process_salary_calculation / simulate / bulk)
    真的 call apply_bonus_supplementary_to_breakdown 且 args 順序正確。

    防衛場景：未來重構時有人改錯 arg 順序、傳錯 emp.employee_id (str 業務碼)
    vs emp.id (int PK)、忘了傳 session 等漂移。
    """

    SPY_PATH = (
        "services.salary.supplementary_premium.apply_bonus_supplementary_to_breakdown"
    )

    def _build_emp(self, session):
        """建一個最小可走 _build_breakdown_for_month 的 Employee。"""
        from datetime import date

        from models.database import Employee

        emp = Employee(
            employee_id="W001",
            name="Wiring測試",
            title="幼兒園教師",
            position="幼兒園教師",
            employee_type="regular",
            base_salary=30000,
            insurance_salary_level=30000,
            hire_date=date(2025, 1, 1),
            is_active=True,
        )
        session.add(emp)
        session.flush()
        return emp

    def test_build_breakdown_for_month_calls_enrichment_with_int_pk(
        self, test_db_session
    ):
        """_build_breakdown_for_month 必須傳 emp.id (int PK)，不是 emp.employee_id (str 業務碼)。"""
        from services.salary_engine import SalaryEngine

        emp = self._build_emp(test_db_session)
        test_db_session.commit()

        engine = SalaryEngine(load_from_db=False)
        with patch(self.SPY_PATH, return_value=0) as spy:
            engine._build_breakdown_for_month(test_db_session, emp, 2026, 6)

        spy.assert_called_once()
        args = spy.call_args.args
        # arg 順序：session, emp_dict, breakdown, year, month, insurance_service, employee_pk
        assert args[0] is test_db_session
        assert isinstance(args[1], dict) and args[1]["name"] == "Wiring測試"
        assert args[3] == 2026
        assert args[4] == 6
        assert args[5] is engine.insurance_service
        assert args[6] == emp.id  # int PK，不是 emp.employee_id="W001" str
        assert isinstance(args[6], int)

    def test_bulk_path_calls_enrichment(self, test_db_session, monkeypatch):
        """bulk path（engine.py:3593 process_salaries_for_month 內）與 simulate.py:324
        都必須 import + call apply_bonus_supplementary_to_breakdown。

        Why source check：兩處重組 fixture 成本不對應風險（wiring 簡單且與
        _build_breakdown_for_month 同 pattern；第一個 test 已實際 exec 驗 arg 對）。
        source check 防 caller 被誤刪 / refactor 漂移即可。
        """
        import inspect

        import api.salary.simulate as simulate_mod
        import services.salary.engine as engine_mod

        simulate_src = inspect.getsource(simulate_mod)
        assert (
            "apply_bonus_supplementary_to_breakdown" in simulate_src
        ), "simulate.py 必須在 calculate_salary 之後接 apply_bonus_supplementary_to_breakdown"

        engine_src = inspect.getsource(engine_mod)
        # engine.py 有 _build_breakdown_for_month + bulk path 兩處 caller
        assert (
            engine_src.count("apply_bonus_supplementary_to_breakdown") >= 2
        ), "engine.py 需有兩處 caller（_build_breakdown_for_month 與 bulk path）"


class TestSupplementaryHealthPersistedToRecord:
    """suprhlt01 migration 加的 column：breakdown 的值要 persist 到 SalaryRecord。"""

    def test_fill_salary_record_writes_supplementary_health(self, test_db_session):
        from datetime import date

        from models.database import Employee, SalaryRecord
        from services.salary.breakdown import SalaryBreakdown
        from services.salary.engine import _fill_salary_record
        from services.salary_engine import SalaryEngine

        emp = Employee(
            employee_id="P001",
            name="Persist測試",
            title="幼兒園教師",
            position="幼兒園教師",
            employee_type="regular",
            base_salary=30000,
            insurance_salary_level=30000,
            hire_date=date(2025, 1, 1),
            is_active=True,
        )
        test_db_session.add(emp)
        test_db_session.flush()

        bd = SalaryBreakdown(
            employee_name=emp.name,
            employee_id=emp.employee_id,
            year=2026,
            month=6,
            gross_salary=120000,
            health_insurance=458 + 1266,  # 既有 + 補充
            supplementary_health_employee=1266,
            total_deduction=3266,
            net_salary=120000 - 3266,
        )
        rec = SalaryRecord(employee_id=emp.id, salary_year=2026, salary_month=6)
        test_db_session.add(rec)

        _fill_salary_record(rec, bd, SalaryEngine(load_from_db=False))
        test_db_session.flush()

        assert rec.supplementary_health_employee == 1266
        assert rec.health_insurance_employee == 458 + 1266
