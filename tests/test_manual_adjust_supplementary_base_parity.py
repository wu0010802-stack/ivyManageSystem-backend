"""manual_adjust 即時重算補充保費的投保基底須與引擎一致（bug #7，salary-suppl 2026-06-16）。

原 _recompute_record_current_supplementary 手刻 emp_dict 用 emp.base_salary（個人底薪），
而正式引擎 _load_emp_dict 走 _resolve_standard_base（職位標準底薪）。當員工個人底薪 ≠
職位標準底薪且未設 insurance_salary_level 時，兩者推導出的 health_insured_salary 不同
→ 補充保費門檻(4×投保額)不同 → manual_adjust 即時重算的當月補充保費/健保/實發暫時算錯。

修法：改用 _salary_engine._load_emp_dict(emp) 建 emp_dict，基底與引擎零漂移。

斷言：manual_adjust 重算後的補充保費，等於用引擎 _load_emp_dict 推導之投保基底
計算的補充保費（而非用 emp.base_salary 個人底薪）。
"""

from __future__ import annotations

from datetime import date

import pytest

from api.salary.manual_adjust import _recompute_record_current_supplementary
from models.database import Employee, SalaryRecord
from services.salary.supplementary_premium import (
    _resolve_health_insured_salary,
    calculate_bonus_supplementary_fee,
)
from services.salary_engine import SalaryEngine


class _FakeInsuranceService:
    supplementary_health_rate = 0.0211
    health_max_insured = 219500

    def get_bracket(self, raw):
        return {"amount": raw}


def _make_engine_with_standard(standard_base: float) -> SalaryEngine:
    """建一個職位標準底薪已載入的引擎（班導 B 級 = standard_base）。"""
    engine = SalaryEngine(load_from_db=False)
    engine._position_salary_standards = {"head_teacher_b": standard_base}
    return engine


def _emp(session, *, base_salary, position, title):
    e = Employee(
        employee_id="P7EMP",
        name="基底測試",
        title=title,
        position=position,
        employee_type="regular",
        base_salary=base_salary,
        insurance_salary_level=None,  # NULL → 投保額沿用 base，凸顯標準底薪 vs 個人底薪差異
        hire_date=date(2025, 1, 1),
        is_active=True,
    )
    session.add(e)
    session.flush()
    return e


def test_recompute_uses_engine_resolved_base_not_personal(test_db_session, monkeypatch):
    """個人底薪 25000 但職位標準 45800：投保基底須採標準 45800（引擎口徑），非 25000。"""
    s = test_db_session
    # 班導 B 級職位標準底薪 = 45800；員工個人底薪填 25000（例如資料殘留或試算）
    engine = _make_engine_with_standard(45800)
    emp = _emp(s, base_salary=25000, position="班導", title="教保員")

    # 把 manual_adjust 取用的引擎 singleton 換成本 test 的 engine
    import api.salary as salary_pkg

    monkeypatch.setattr(salary_pkg, "_salary_engine", engine, raising=False)

    # 引擎口徑下投保基底（透過 _load_emp_dict → _resolve_standard_base）
    emp_dict = engine._load_emp_dict(emp)
    assert emp_dict["base_salary"] == 45800, "前置：引擎應解出職位標準底薪 45800"
    engine_insured = _resolve_health_insured_salary(emp_dict, _FakeInsuranceService())
    assert engine_insured == 45800.0

    # 前月累計刻意落在兩種投保基底門檻之間（4×25000=100000 < 150000 < 4×45800=183200），
    # 使 basis=max(ytd, threshold) 對兩基底取不同值 → fee 明確依賴投保基底（凸顯 bug）。
    s.add(
        SalaryRecord(
            employee_id=emp.id, salary_year=2026, salary_month=2, festival_bonus=150000
        )
    )
    rec = SalaryRecord(
        employee_id=emp.id,
        salary_year=2026,
        salary_month=6,
        festival_bonus=100000,
        health_insurance_employee=600,
        supplementary_health_employee=0,
    )
    s.add(rec)
    s.flush()

    _recompute_record_current_supplementary(s, rec, _FakeInsuranceService())

    # 期望值：以引擎投保基底 45800 計算的補充保費
    expected = calculate_bonus_supplementary_fee(
        s,
        emp.id,
        2026,
        6,
        breakdown_bonus_total=100000,
        health_insured_salary=engine_insured,  # 45800（引擎口徑）
        rate=0.0211,
    )
    # 對照：若沿用舊 bug 的個人底薪 25000，門檻更低、fee 會偏大 → 兩者不同
    wrong = calculate_bonus_supplementary_fee(
        s,
        emp.id,
        2026,
        6,
        breakdown_bonus_total=100000,
        health_insured_salary=25000.0,  # 舊 bug 的個人底薪
        rate=0.0211,
    )
    assert expected != wrong, "前置：標準底薪與個人底薪須給出不同 fee，否則無法分辨 bug"
    assert rec.supplementary_health_employee == expected
    assert rec.health_insurance_employee == 600 + expected


def test_recompute_health_exempt_skips(test_db_session, monkeypatch):
    """#6 + #7 交集：health_exempt 員工經 manual_adjust 路徑亦不扣補充保費。"""
    s = test_db_session
    engine = _make_engine_with_standard(45800)
    emp = Employee(
        employee_id="P7EXEMPT",
        name="豁免測試",
        title="教保員",
        position="班導",
        employee_type="regular",
        base_salary=45800,
        insurance_salary_level=None,
        health_exempt=True,
        hire_date=date(2025, 1, 1),
        is_active=True,
    )
    s.add(emp)
    s.flush()

    import api.salary as salary_pkg

    monkeypatch.setattr(salary_pkg, "_salary_engine", engine, raising=False)

    s.add(
        SalaryRecord(
            employee_id=emp.id, salary_year=2026, salary_month=2, festival_bonus=300000
        )
    )
    rec = SalaryRecord(
        employee_id=emp.id,
        salary_year=2026,
        salary_month=6,
        festival_bonus=100000,
        health_insurance_employee=0,
        supplementary_health_employee=0,
    )
    s.add(rec)
    s.flush()

    _recompute_record_current_supplementary(s, rec, _FakeInsuranceService())

    assert rec.supplementary_health_employee == 0
    assert rec.health_insurance_employee == 0
