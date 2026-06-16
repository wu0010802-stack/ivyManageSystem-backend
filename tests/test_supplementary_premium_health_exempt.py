"""二代健保補充保費 — health_exempt 員工豁免（bug #6，bug hunt salary-suppl 2026-06-16）。

health_exempt 員工（公保/老人健保等由其他管道投保健保者）一般健保本人保費已歸零
（insurance_service.calculate：health_exempt → health_emp=0）。但獎金路徑的補充保費
原未檢查 health_exempt，仍以投保薪資為門檻基底課徵 2.11%，與「免扣一般健保」口徑矛盾。

業務口徑假設：補充保費對 health_exempt 是否法定應扣有爭議；本修補預設「比照一般健保
免扣」（health_exempt → 補充保費回 0），待業主確認。

覆蓋：
1. apply_bonus_supplementary_to_breakdown：health_exempt=True 時 fee=0、breakdown 四欄不動
2. _resolve_health_insured_salary：health_exempt=True 回 0.0（共用單點，亦覆蓋 manual_adjust 路徑）
3. health_exempt=False（或缺鍵）時行為不變（仍照常課徵）
"""

from __future__ import annotations

from models.salary import SalaryRecord
from services.salary.breakdown import SalaryBreakdown
from services.salary.supplementary_premium import (
    _resolve_health_insured_salary,
    apply_bonus_supplementary_to_breakdown,
)

EMP_ID = 2002


class _FakeInsuranceService:
    supplementary_health_rate = 0.0211
    health_max_insured = 219500

    def get_bracket(self, raw):
        return {"amount": raw}


def _add_salary_record(session, *, year, month, **bonus_fields):
    rec = SalaryRecord(employee_id=EMP_ID, salary_year=year, salary_month=month)
    for k, v in bonus_fields.items():
        setattr(rec, k, v)
    session.add(rec)
    session.flush()
    return rec


def _make_breakdown(**overrides):
    bd = SalaryBreakdown(
        employee_name="Test",
        employee_id="E002",
        year=2026,
        month=6,
        base_salary=30000,
        gross_salary=120000,
        health_insurance=458,
        total_deduction=2000,
    )
    for k, v in overrides.items():
        setattr(bd, k, v)
    return bd


def test_health_exempt_employee_pays_no_bonus_supplementary(test_db_session):
    """health_exempt=True：原本會扣 1266 的情境，現應 fee=0、breakdown 不動。"""
    _add_salary_record(test_db_session, year=2026, month=2, festival_bonus=100000)
    bd = _make_breakdown(festival_bonus=80000)
    emp_dict = {
        "employee_type": "regular",
        "base_salary": 30000,
        "insurance_salary": 30000,
        "health_insured_salary": None,
        "health_exempt": True,
    }
    fee = apply_bonus_supplementary_to_breakdown(
        test_db_session,
        emp_dict,
        bd,
        2026,
        6,
        _FakeInsuranceService(),
        EMP_ID,
    )
    assert fee == 0
    # breakdown 四欄不得被 mutate
    assert bd.health_insurance == 458
    assert bd.total_deduction == 2000
    assert (bd.supplementary_health_employee or 0) == 0


def test_non_exempt_employee_still_charged(test_db_session):
    """對照組：health_exempt=False 時行為不變（仍課 1266）。"""
    _add_salary_record(test_db_session, year=2026, month=2, festival_bonus=100000)
    bd = _make_breakdown(festival_bonus=80000)
    emp_dict = {
        "employee_type": "regular",
        "base_salary": 30000,
        "insurance_salary": 30000,
        "health_insured_salary": None,
        "health_exempt": False,
    }
    fee = apply_bonus_supplementary_to_breakdown(
        test_db_session,
        emp_dict,
        bd,
        2026,
        6,
        _FakeInsuranceService(),
        EMP_ID,
    )
    assert fee == 1266
    assert bd.supplementary_health_employee == 1266


def test_resolve_health_insured_salary_zero_for_exempt():
    """共用單點：_resolve_health_insured_salary 對 health_exempt 回 0.0，
    使 manual_adjust 直呼路徑（_recompute_record_current_supplementary）亦豁免。"""
    emp_dict = {
        "employee_type": "regular",
        "base_salary": 30000,
        "insurance_salary": 30000,
        "health_insured_salary": None,
        "health_exempt": True,
    }
    assert _resolve_health_insured_salary(emp_dict, _FakeInsuranceService()) == 0.0


def test_resolve_health_insured_salary_nonzero_for_non_exempt():
    emp_dict = {
        "employee_type": "regular",
        "base_salary": 30000,
        "insurance_salary": 30000,
        "health_insured_salary": None,
        "health_exempt": False,
    }
    assert _resolve_health_insured_salary(emp_dict, _FakeInsuranceService()) == 30000.0
