"""額外加給（extra_allowance）手填欄位測試。

涵蓋：
- recompute_record_totals 把 extra_allowance 併入 gross
- extra_allowance 不在二代健保補充保費年累計欄位
- snapshot 反射欄位含 extra_allowance（避免快照漏欄）
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.salary.totals import recompute_record_totals


def _blank_record(**kw):
    """最小 SalaryRecord 替身：recompute_record_totals 只讀欄位、寫 totals。"""
    fields = dict(
        base_salary=30000,
        hourly_total=0,
        performance_bonus=0,
        special_bonus=0,
        supervisor_dividend=0,
        meeting_overtime_pay=0,
        birthday_bonus=0,
        overtime_pay=0,
        extra_allowance=0,
        labor_insurance_employee=0,
        health_insurance_employee=0,
        pension_employee=0,
        late_deduction=0,
        early_leave_deduction=0,
        missing_punch_deduction=0,
        leave_deduction=0,
        absence_deduction=0,
        other_deduction=0,
        festival_bonus=0,
        overtime_bonus=0,
        gross_salary=0,
        total_deduction=0,
        net_salary=0,
        bonus_amount=0,
        bonus_separate=False,
    )
    fields.update(kw)
    return SimpleNamespace(**fields)


def test_extra_allowance_included_in_gross():
    rec = _blank_record(base_salary=30000, extra_allowance=1241)
    recompute_record_totals(rec)
    assert rec.gross_salary == 31241
    assert rec.net_salary == 31241  # 無扣款


def test_extra_allowance_zero_no_effect():
    rec = _blank_record(base_salary=30000, extra_allowance=0)
    recompute_record_totals(rec)
    assert rec.gross_salary == 30000


def test_extra_allowance_not_in_supplementary_ytd_fields():
    from services.salary.supplementary_premium import BONUS_FIELDS_FOR_YTD

    assert "extra_allowance" not in BONUS_FIELDS_FOR_YTD


def test_snapshot_payload_columns_include_extra_allowance():
    from services.finance.salary_snapshot_service import _PAYLOAD_COLUMNS

    assert "extra_allowance" in _PAYLOAD_COLUMNS
    assert "extra_allowance_label" in _PAYLOAD_COLUMNS


def test_salary_slip_earnings_table_includes_extra_allowance():
    """薪資單應領表格在 extra_allowance > 0 時多一列，顯示名目與金額。"""
    from services.finance.salary_slip import _build_earnings_table

    rec = _blank_record(
        base_salary=30000, gross_salary=31241, extra_allowance=1241
    )
    rec.extra_allowance_label = "值週"
    table = _build_earnings_table(rec, "Helvetica", lambda v: f"{float(v):,.0f}")
    flat = [str(c) for row in table._cellvalues for c in row]
    assert any("值週" in c for c in flat), f"名目未出現：{flat}"
    assert any("1,241" in c for c in flat), f"金額未出現：{flat}"


def test_salary_slip_earnings_fallback_label():
    """名目空白時 fallback 顯示「額外加給」。"""
    from services.finance.salary_slip import _build_earnings_table

    rec = _blank_record(base_salary=30000, gross_salary=30500, extra_allowance=500)
    rec.extra_allowance_label = None
    table = _build_earnings_table(rec, "Helvetica", lambda v: f"{float(v):,.0f}")
    flat = [str(c) for row in table._cellvalues for c in row]
    assert any("額外加給" in c for c in flat), f"fallback 名目未出現：{flat}"


def test_salary_slip_no_extra_row_when_zero():
    """extra_allowance = 0 時不應多出該列。"""
    from services.finance.salary_slip import _build_earnings_table

    rec = _blank_record(base_salary=30000, gross_salary=30000, extra_allowance=0)
    rec.extra_allowance_label = None
    table = _build_earnings_table(rec, "Helvetica", lambda v: f"{float(v):,.0f}")
    flat = [str(c) for row in table._cellvalues for c in row]
    assert not any("額外加給" in c for c in flat)
