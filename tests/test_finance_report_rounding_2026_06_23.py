"""財報金額彙總一律用 round_half_up（政府/勞健保/PG NUMERIC 標準），不得 int() 截斷。

qa-loop #10（2026-06-23）：finance_report_service 對 SQL func.sum 結果與
Numeric(12,2) 金額（VendorPayment.amount、雇主勞健保/勞退費率乘積）用 int()
朝零截斷，與姊妹報表 api/reports.py 的 round_half_up 口徑不一致，且系統性少計
（截斷恆向下，每月每類最多近 NT$0.99）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from models.database import Employee, SalaryRecord
from models.vendor_payment import VendorPayment
from services.finance_report_service import (
    _month_totals_from,
    get_salary_expense_by_month,
    get_vendor_payment_detail,
    get_vendor_payment_expense_by_month,
)


def test_month_totals_from_rounds_half_up_not_truncate():
    """純函式：分數總和 .5 以上應進位，不截斷。"""
    assert _month_totals_from([(3, Decimal("200.99"))]) == {3: 201}
    assert _month_totals_from([(5, Decimal("100.50"))]) == {5: 101}
    # .4 以下維持不變（不過度進位）
    assert _month_totals_from([(4, Decimal("100.40"))]) == {4: 100}
    # None 視為 0
    assert _month_totals_from([(6, None)]) == {6: 0}


def test_vendor_expense_rounds_half_up(test_db_session):
    """廠商付款月彙總（Numeric(12,2)）：200.99 應 round_half_up→201，不得截斷成 200。"""
    s = test_db_session
    s.add_all(
        [
            VendorPayment(
                payment_date=date(2026, 3, 5),
                vendor_name="A",
                amount=Decimal("100.50"),
                payment_method="cash",
                status="signed",
            ),
            VendorPayment(
                payment_date=date(2026, 3, 9),
                vendor_name="B",
                amount=Decimal("100.49"),
                payment_method="cash",
                status="signed",
            ),
        ]
    )
    s.commit()
    out = get_vendor_payment_expense_by_month(s, 2026)
    assert (
        out[3] == 201
    ), f"200.99 應 round_half_up→201（截斷會得 200），實得 {out.get(3)}"


def test_salary_employer_benefit_rounds_half_up(test_db_session):
    """雇主負擔（費率乘積帶小數）月彙總須 round_half_up，不得截斷少計。"""
    s = test_db_session
    emp = Employee(
        employee_id="E_RND",
        name="T",
        employee_type="regular",
        is_active=True,
    )
    s.add(emp)
    s.flush()
    s.add(
        SalaryRecord(
            employee_id=emp.id,
            salary_year=2026,
            salary_month=3,
            gross_salary=Decimal("30000"),
            labor_insurance_employer=Decimal("100.50"),
            health_insurance_employer=Decimal("100.49"),
            pension_employer=Decimal("0"),
            net_salary=Decimal("0"),
            total_deduction=Decimal("0"),
            is_finalized=True,
        )
    )
    s.commit()
    out = get_salary_expense_by_month(s, 2026)
    assert out[3]["employer_benefit"] == 201, (
        f"100.50+100.49=200.99 應 round_half_up→201（截斷會得 200），"
        f"實得 {out[3]['employer_benefit']}"
    )


def test_vendor_payment_detail_amount_rounds_half_up(test_db_session):
    """廠商付款明細列金額（Numeric(12,2)）須 round_half_up，不得截斷。"""
    s = test_db_session
    s.add(
        VendorPayment(
            payment_date=date(2026, 3, 5),
            vendor_name="A",
            amount=Decimal("100.50"),
            payment_method="cash",
            status="signed",
        )
    )
    s.commit()
    rows = get_vendor_payment_detail(s, 2026, 3)
    assert (
        rows[0]["amount"] == 101
    ), f"100.50 應 round_half_up→101，實得 {rows[0]['amount']}"
