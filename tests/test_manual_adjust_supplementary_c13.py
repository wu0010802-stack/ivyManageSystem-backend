"""manual_adjust 改獎金後當月補充保費即時重算（C13，bug hunt money-auth 2026-06-14）。

原 manual_adjust 改 BONUS_FIELDS_FOR_YTD 後只重算 total/net、標 stale，不重算當月補充
保費 → manual_adjust 到下次引擎重算之間 total_deduction/net 暫時失準。修法：重用引擎
相同純函式（_resolve_health_insured_salary + calculate_bonus_supplementary_fee）即時
重算當月補充保費並併回 health_insurance_employee；無注入 insurance_service 時優雅跳過。
"""

from datetime import date

from api.salary.manual_adjust import _recompute_record_current_supplementary
from models.database import Employee, SalaryRecord


class _FakeInsuranceService:
    supplementary_health_rate = 0.0211

    def get_bracket(self, raw):
        return {"amount": raw}


def _emp(session):
    e = Employee(
        employee_id="C13EMP",
        name="C13測試",
        title="幼兒園教師",
        position="幼兒園教師",
        employee_type="regular",
        base_salary=30000,
        insurance_salary_level=30000,
        hire_date=date(2025, 1, 1),
        is_active=True,
    )
    session.add(e)
    session.flush()
    return e


def test_recompute_updates_supplementary_and_health(test_db_session):
    s = test_db_session
    emp = _emp(s)
    # 前月累計（ytd_before）= 100000
    s.add(
        SalaryRecord(
            employee_id=emp.id, salary_year=2026, salary_month=2, festival_bonus=100000
        )
    )
    # 當月 record：festival 80000，但補充保費尚未算（stale）
    rec = SalaryRecord(
        employee_id=emp.id,
        salary_year=2026,
        salary_month=6,
        festival_bonus=80000,
        health_insurance_employee=458,
        supplementary_health_employee=0,
    )
    s.add(rec)
    s.flush()

    _recompute_record_current_supplementary(s, rec, _FakeInsuranceService())

    # threshold=120000, prior=100000, this=80000 → ytd_after=180000, basis=120000
    # excess=60000 → 60000×0.0211=1266
    assert rec.supplementary_health_employee == 1266
    assert rec.health_insurance_employee == 458 + 1266


def test_recompute_idempotent(test_db_session):
    s = test_db_session
    emp = _emp(s)
    s.add(
        SalaryRecord(
            employee_id=emp.id, salary_year=2026, salary_month=2, festival_bonus=100000
        )
    )
    rec = SalaryRecord(
        employee_id=emp.id,
        salary_year=2026,
        salary_month=6,
        festival_bonus=80000,
        health_insurance_employee=458,
        supplementary_health_employee=0,
    )
    s.add(rec)
    s.flush()
    _recompute_record_current_supplementary(s, rec, _FakeInsuranceService())
    h1 = rec.health_insurance_employee
    # 第二次 new_fee==old_fee → 不再加（不 double-count）
    _recompute_record_current_supplementary(s, rec, _FakeInsuranceService())
    assert rec.health_insurance_employee == h1
    assert rec.supplementary_health_employee == 1266


def test_recompute_noop_when_no_service(test_db_session):
    s = test_db_session
    emp = _emp(s)
    rec = SalaryRecord(
        employee_id=emp.id,
        salary_year=2026,
        salary_month=6,
        festival_bonus=80000,
        health_insurance_employee=458,
        supplementary_health_employee=0,
    )
    s.add(rec)
    s.flush()
    # insurance_service 為 None → 優雅跳過（沿用 stale+gate 收斂），不動 record
    _recompute_record_current_supplementary(s, rec, None)
    assert rec.supplementary_health_employee == 0
    assert rec.health_insurance_employee == 458
