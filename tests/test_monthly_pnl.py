"""monthly_pnl_service 與 /api/reports/monthly-pnl 端點測試。

對齊 test_finance_report.py 的 fixture pattern：SQLite in-memory + auto-mirror
StudentFeePayment（讓僅建 StudentFeeRecord 的舊測試也能驅動 payment 流水）。
"""

import os
import sys
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import (
    router as auth_router,
    _account_failures,
    _ip_attempts,
)
from api.reports import router as reports_router
from models.classroom import Classroom
from models.database import (
    Base,
    Employee,
    User,
    ActivityPaymentRecord,
    SalaryRecord,
)
from models.fees import StudentFeePayment, StudentFeeRecord, StudentFeeRefund
from models.vendor_payment import VendorPayment
from services.monthly_pnl_service import build_monthly_pnl
from utils.auth import hash_password


def _register_auto_mirror_payment(session_factory):
    """StudentFeeRecord.amount_paid > 0 時自動 mirror 一筆 StudentFeePayment。

    與 test_finance_report.py 相同邏輯，避免重複 fixture 維護成本。
    """

    @sa_event.listens_for(session_factory, "after_flush")
    def _mirror(session, flush_context):
        for obj in list(session.new):
            if not isinstance(obj, StudentFeeRecord):
                continue
            if (obj.amount_paid or 0) <= 0 or not obj.payment_date:
                continue
            has_existing = any(
                isinstance(o, StudentFeePayment) and o.record_id == obj.id
                for o in list(session.new)
            )
            if has_existing:
                continue
            session.add(
                StudentFeePayment(
                    record_id=obj.id,
                    amount=obj.amount_paid,
                    payment_date=obj.payment_date,
                    payment_method=obj.payment_method or "現金",
                    notes="（測試 auto-mirror）",
                    operator="test",
                )
            )


@pytest.fixture
def pnl_client(tmp_path):
    db_path = tmp_path / "pnl.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    _register_auto_mirror_payment(session_factory)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(reports_router)
    with TestClient(app) as client:
        yield client, session_factory
    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_e
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_admin(sf, client):
    with sf() as s:
        s.add(
            User(
                username="pnl_admin",
                password_hash=hash_password("PnlPass123"),
                role="admin",
                permissions=-1,
                is_active=True,
                must_change_password=False,
            )
        )
        s.commit()
    r = client.post(
        "/api/auth/login", json={"username": "pnl_admin", "password": "PnlPass123"}
    )
    assert r.status_code == 200


def _row(sections: list, section_key: str, row_key: str) -> dict:
    """測試輔助：從 sections 找指定 section_key 下的 row_key。"""
    for sec in sections:
        if sec["key"] != section_key:
            continue
        for row in sec["rows"]:
            if row["key"] == row_key:
                return row
    raise AssertionError(f"row not found: {section_key}.{row_key}")


# ── Aggregator 單元測試 ────────────────────────────────────────────────────


class TestMonthlyPnLAggregator:
    def test_aggregator_empty_year(self, pnl_client):
        """空 DB 回傳所有列都是 0，且 monthly 永遠長 12。"""
        _, sf = pnl_client
        with sf() as s:
            data = build_monthly_pnl(s, 2026)

        assert data["year"] == 2026
        # 每個 amount row 的 monthly 長度 12 + 全 0
        for sec in data["sections"]:
            for row in sec["rows"]:
                assert len(row["monthly"]) == 12, f"{row['key']} monthly len != 12"
                assert all(v == 0 for v in row["monthly"]), f"{row['key']} not all zero"

        # totals 全 0
        for k in ("income_total", "refund_total", "expense_total", "net_cashflow"):
            assert data["totals"][k]["total"] == 0
            assert len(data["totals"][k]["monthly"]) == 12
            assert all(v == 0 for v in data["totals"][k]["monthly"])

        # pending_items 非空
        assert isinstance(data["pending_items"], list)
        assert len(data["pending_items"]) > 0

    def test_aggregator_income_split_by_method(self, pnl_client):
        """3 筆 payment（現金/轉帳/其他）正確落入 cash/bank_transfer/other_method。"""
        _, sf = pnl_client
        with sf() as s:
            s.add_all(
                [
                    StudentFeeRecord(
                        id=1,
                        student_id=1,
                        period="2026-1",
                        amount_due=10000,
                        amount_paid=0,
                        status="unpaid",
                        student_name="A",
                        fee_item_name="月費",
                        fee_type="monthly",
                    ),
                    StudentFeeRecord(
                        id=2,
                        student_id=2,
                        period="2026-1",
                        amount_due=10000,
                        amount_paid=0,
                        status="unpaid",
                        student_name="B",
                        fee_item_name="月費",
                        fee_type="monthly",
                    ),
                    StudentFeeRecord(
                        id=3,
                        student_id=3,
                        period="2026-1",
                        amount_due=10000,
                        amount_paid=0,
                        status="unpaid",
                        student_name="C",
                        fee_item_name="月費",
                        fee_type="monthly",
                    ),
                ]
            )
            s.commit()
        with sf() as s:
            s.add_all(
                [
                    StudentFeePayment(
                        record_id=1,
                        amount=5000,
                        payment_date=date(2026, 3, 10),
                        payment_method="現金",
                        operator="t",
                    ),
                    StudentFeePayment(
                        record_id=2,
                        amount=7000,
                        payment_date=date(2026, 3, 12),
                        payment_method="轉帳",
                        operator="t",
                    ),
                    StudentFeePayment(
                        record_id=3,
                        amount=2000,
                        payment_date=date(2026, 3, 15),
                        payment_method="check",  # 非 cash/transfer 視同其他
                        operator="t",
                    ),
                ]
            )
            s.commit()
        with sf() as s:
            data = build_monthly_pnl(s, 2026)

        assert _row(data["sections"], "income", "income_cash")["monthly"][2] == 5000
        assert _row(data["sections"], "income", "income_transfer")["monthly"][2] == 7000
        assert (
            _row(data["sections"], "income", "income_other_method")["monthly"][2]
            == 2000
        )
        # subtotal = 5000+7000+2000（無 activity）
        assert (
            _row(data["sections"], "income", "income_subtotal")["monthly"][2] == 14000
        )

    def test_aggregator_income_split_by_fee_type(self, pnl_client):
        """4 筆 payment（registration/material/monthly/tuition）落入正確 by-fee-type 桶。

        monthly 與 tuition 應合併進 monthly_tuition。
        """
        _, sf = pnl_client
        with sf() as s:
            s.add_all(
                [
                    StudentFeeRecord(
                        id=1,
                        student_id=1,
                        period="2026-1",
                        amount_due=8000,
                        amount_paid=8000,
                        status="paid",
                        payment_date=date(2026, 4, 5),
                        payment_method="現金",
                        student_name="A",
                        fee_item_name="註冊費",
                        fee_type="registration",
                    ),
                    StudentFeeRecord(
                        id=2,
                        student_id=2,
                        period="2026-1",
                        amount_due=3000,
                        amount_paid=3000,
                        status="paid",
                        payment_date=date(2026, 4, 6),
                        payment_method="轉帳",
                        student_name="B",
                        fee_item_name="耗材",
                        fee_type="material",
                    ),
                    StudentFeeRecord(
                        id=3,
                        student_id=3,
                        period="2026-1",
                        amount_due=10000,
                        amount_paid=10000,
                        status="paid",
                        payment_date=date(2026, 4, 7),
                        payment_method="現金",
                        student_name="C",
                        fee_item_name="月費",
                        fee_type="monthly",
                    ),
                    StudentFeeRecord(
                        id=4,
                        student_id=4,
                        period="2026-1",
                        amount_due=15000,
                        amount_paid=15000,
                        status="paid",
                        payment_date=date(2026, 4, 8),
                        payment_method="轉帳",
                        student_name="D",
                        fee_item_name="學費",
                        fee_type="tuition",
                    ),
                ]
            )
            s.commit()
        with sf() as s:
            data = build_monthly_pnl(s, 2026)

        assert (
            _row(data["sections"], "income", "income_registration")["monthly"][3]
            == 8000
        )
        assert _row(data["sections"], "income", "income_material")["monthly"][3] == 3000
        # monthly + tuition 合併
        assert (
            _row(data["sections"], "income", "income_monthly_tuition")["monthly"][3]
            == 25000
        )

    def test_aggregator_activity_payment_and_refund(self, pnl_client):
        """ActivityPaymentRecord type=payment 進 income_activity；refund 進 income_refund。"""
        _, sf = pnl_client
        with sf() as s:
            s.add_all(
                [
                    ActivityPaymentRecord(
                        registration_id=1,
                        type="payment",
                        amount=3000,
                        payment_date=date(2026, 5, 10),
                    ),
                    ActivityPaymentRecord(
                        registration_id=1,
                        type="refund",
                        amount=500,
                        payment_date=date(2026, 5, 20),
                    ),
                ]
            )
            s.commit()
        with sf() as s:
            data = build_monthly_pnl(s, 2026)

        assert _row(data["sections"], "income", "income_activity")["monthly"][4] == 3000
        assert _row(data["sections"], "income", "income_refund")["monthly"][4] == 500
        # subtotal 只含 by-method 切片（cash+transfer+other+activity）
        assert _row(data["sections"], "income", "income_subtotal")["monthly"][4] == 3000

    def test_aggregator_salary_breakdown(self, pnl_client):
        """1 筆 SalaryRecord 驗證 8 個人事細項 + subtotal 公式正確。

        關鍵：gross_salary 已含 overtime_pay 與 supervisor_dividend；
        base_salary 行 = gross - overtime_pay - supervisor_dividend。
        subtotal 應等於 gross + festival_bonus + overtime_bonus + 3 雇主保險。
        """
        _, sf = pnl_client
        with sf() as s:
            emp = Employee(
                employee_id="E1",
                name="T",
                base_salary=30000,
                employee_type="regular",
                is_active=True,
            )
            s.add(emp)
            s.flush()
            s.add(
                SalaryRecord(
                    employee_id=emp.id,
                    salary_year=2026,
                    salary_month=6,
                    gross_salary=42000,  # 含 overtime_pay(2000) + supervisor_dividend(3000)
                    festival_bonus=5000,
                    overtime_bonus=1000,
                    overtime_pay=2000,
                    supervisor_dividend=3000,
                    labor_insurance_employer=2500,
                    health_insurance_employer=1400,
                    pension_employer=1800,
                    net_salary=35000,
                    total_deduction=7000,
                )
            )
            s.commit()
        with sf() as s:
            data = build_monthly_pnl(s, 2026)

        rows = data["sections"]
        # base = 42000 - 2000 - 3000 = 37000
        assert (
            _row(rows, "personnel_expense", "personnel_base_salary")["monthly"][5]
            == 37000
        )
        assert (
            _row(rows, "personnel_expense", "personnel_festival_bonus")["monthly"][5]
            == 5000
        )
        assert (
            _row(rows, "personnel_expense", "personnel_overtime_bonus")["monthly"][5]
            == 1000
        )
        assert (
            _row(rows, "personnel_expense", "personnel_overtime_pay")["monthly"][5]
            == 2000
        )
        # personnel_other_bonus = supervisor_dividend
        assert (
            _row(rows, "personnel_expense", "personnel_other_bonus")["monthly"][5]
            == 3000
        )
        assert (
            _row(rows, "personnel_expense", "personnel_labor_insurance")["monthly"][5]
            == 2500
        )
        assert (
            _row(rows, "personnel_expense", "personnel_health_insurance")["monthly"][5]
            == 1400
        )
        assert (
            _row(rows, "personnel_expense", "personnel_pension")["monthly"][5] == 1800
        )

        # subtotal = base + festival + ot_bonus + ot_pay + sup_div + 3 insurance
        #         = 37000 + 5000 + 1000 + 2000 + 3000 + 2500 + 1400 + 1800
        #         = 53700
        # 等價於 gross(42000) + festival(5000) + ot_bonus(1000) + 雇主三項(5700) = 53700
        assert (
            _row(rows, "personnel_expense", "personnel_subtotal")["monthly"][5] == 53700
        )

    def test_aggregator_vendor_payment(self, pnl_client):
        """variable_vendor 取 VendorPayment.amount 不分 status；subtotal 等同 vendor。"""
        _, sf = pnl_client
        with sf() as s:
            s.add_all(
                [
                    VendorPayment(
                        payment_date=date(2026, 7, 5),
                        vendor_name="A",
                        amount=1200,
                        payment_method="cash",
                        status="signed",
                    ),
                    VendorPayment(
                        payment_date=date(2026, 7, 10),
                        vendor_name="B",
                        amount=800,
                        payment_method="bank_transfer",
                        status="pending",
                    ),
                ]
            )
            s.commit()
        with sf() as s:
            data = build_monthly_pnl(s, 2026)

        assert (
            _row(data["sections"], "variable_expense", "variable_vendor")["monthly"][6]
            == 2000
        )
        assert (
            _row(data["sections"], "variable_expense", "variable_subtotal")["monthly"][
                6
            ]
            == 2000
        )

    def test_aggregator_totals_and_net(self, pnl_client):
        """混合：tuition 收入 + activity 收入 + 退款 + salary + vendor，net_cashflow 公式驗算。"""
        _, sf = pnl_client
        with sf() as s:
            emp = Employee(
                employee_id="E1",
                name="T",
                base_salary=30000,
                employee_type="regular",
                is_active=True,
            )
            s.add(emp)
            s.flush()
            # 5 月：收入 10000 + 才藝 2000 = 12000；退款 1500；薪資雇主 5700 + gross 30000 = 35700；vendor 1000
            s.add_all(
                [
                    StudentFeeRecord(
                        student_id=1,
                        period="2026-1",
                        amount_due=10000,
                        amount_paid=10000,
                        status="paid",
                        payment_date=date(2026, 5, 1),
                        payment_method="現金",
                        student_name="A",
                        fee_item_name="月費",
                        fee_type="monthly",
                    ),
                    ActivityPaymentRecord(
                        registration_id=1,
                        type="payment",
                        amount=2000,
                        payment_date=date(2026, 5, 3),
                    ),
                    StudentFeeRefund(
                        record_id=1,
                        amount=1500,
                        reason="x",
                        refunded_at=datetime(2026, 5, 10, 9, 0),
                        refunded_by="admin",
                        idempotency_key="r1",
                    ),
                    SalaryRecord(
                        employee_id=emp.id,
                        salary_year=2026,
                        salary_month=5,
                        gross_salary=30000,
                        labor_insurance_employer=2500,
                        health_insurance_employer=1400,
                        pension_employer=1800,
                        net_salary=25000,
                        total_deduction=5000,
                    ),
                    VendorPayment(
                        payment_date=date(2026, 5, 15),
                        vendor_name="V",
                        amount=1000,
                        payment_method="cash",
                        status="signed",
                    ),
                ]
            )
            s.commit()
        with sf() as s:
            data = build_monthly_pnl(s, 2026)

        m_idx = 4  # 5 月
        income = data["totals"]["income_total"]["monthly"][m_idx]
        refund = data["totals"]["refund_total"]["monthly"][m_idx]
        expense = data["totals"]["expense_total"]["monthly"][m_idx]
        net = data["totals"]["net_cashflow"]["monthly"][m_idx]

        assert income == 12000  # 現金 10000 + 才藝 2000
        assert refund == 1500
        # expense = personnel_subtotal + variable_subtotal
        # personnel: base(30000-0-0=30000) + festival 0 + ot_bonus 0 + ot_pay 0 + supdiv 0 + 5700 雇主 = 35700
        # variable: 1000
        assert expense == 36700
        assert net == income - refund - expense
        assert net == 12000 - 1500 - 36700

    def test_aggregator_classroom_count_and_insured_employees(self, pnl_client):
        """統計列：班級數 = 全年 baseline；投保員工數依 hire/resign date 過濾。"""
        _, sf = pnl_client
        with sf() as s:
            # 2 個 active classroom + 1 個 inactive
            s.add_all(
                [
                    Classroom(
                        school_year=114,
                        semester=1,
                        name="大班",
                        grade_id=None,
                        is_active=True,
                    ),
                    Classroom(
                        school_year=114,
                        semester=1,
                        name="中班",
                        grade_id=None,
                        is_active=True,
                    ),
                    Classroom(
                        school_year=114,
                        semester=1,
                        name="舊班",
                        grade_id=None,
                        is_active=False,
                    ),
                ]
            )
            # 員工 A：2026-01-01 入職、無離職、labor_insured_salary=30000 → 全年 12 月皆計入
            # 員工 B：2025-12-31 入職、2026-06-15 離職 → 1-6 月計入；7 月起不計
            # 員工 C：2026-01-01 入職、labor_insured_salary=NULL → 不計入
            # 員工 D：2026-05-01 入職、無離職、labor_insured_salary=25000 → 5-12 月計入
            s.add_all(
                [
                    Employee(
                        employee_id="A",
                        name="A",
                        base_salary=30000,
                        employee_type="regular",
                        is_active=True,
                        hire_date=date(2026, 1, 1),
                        labor_insured_salary=30000,
                    ),
                    Employee(
                        employee_id="B",
                        name="B",
                        base_salary=30000,
                        employee_type="regular",
                        is_active=False,
                        hire_date=date(2025, 12, 31),
                        resign_date=date(2026, 6, 15),
                        labor_insured_salary=30000,
                    ),
                    Employee(
                        employee_id="C",
                        name="C",
                        base_salary=30000,
                        employee_type="regular",
                        is_active=True,
                        hire_date=date(2026, 1, 1),
                        labor_insured_salary=None,
                    ),
                    Employee(
                        employee_id="D",
                        name="D",
                        base_salary=25000,
                        employee_type="regular",
                        is_active=True,
                        hire_date=date(2026, 5, 1),
                        labor_insured_salary=25000,
                    ),
                ]
            )
            s.commit()

        with sf() as s:
            data = build_monthly_pnl(s, 2026)

        classroom_row = _row(data["sections"], "stats", "classroom_count")
        # baseline 全年 12 月皆 2
        assert classroom_row["monthly"] == [2] * 12
        # 統計列 total=None（不算合計）
        assert classroom_row["total"] is None
        assert classroom_row["unit"] == "class"

        insured_row = _row(data["sections"], "stats", "insured_employee_count")
        # 1-4 月：A + B = 2
        # 5 月：A + B + D = 3
        # 6 月：A + B + D = 3（B 6/15 離職，resign_date > 6/1 仍計入）
        # 7-12 月：A + D = 2
        assert insured_row["monthly"][:4] == [2, 2, 2, 2]
        assert insured_row["monthly"][4] == 3
        assert insured_row["monthly"][5] == 3
        assert insured_row["monthly"][6:] == [2] * 6
        assert insured_row["total"] is None
        assert insured_row["unit"] == "person"

    # ── Phase 2 新增測試 ────────────────────────────────────────────────

    def test_aggregator_art_teacher_split(self, pnl_client):
        """才藝老師（hourly）的 base 薪資切出至 personnel_art_teacher_hourly。

        regular 員工 → personnel_base_salary；hourly 員工 → personnel_art_teacher_hourly；
        其他列（festival／overtime／勞健保／勞退）仍跨 role 加總。
        """
        from models.monthly_fixed_cost import MonthlyFixedCost  # noqa: F401

        _, sf = pnl_client
        with sf() as s:
            reg_emp = Employee(
                employee_id="REG",
                name="正職",
                base_salary=30000,
                employee_type="regular",
                is_active=True,
            )
            hr_emp = Employee(
                employee_id="HR",
                name="才藝老師",
                base_salary=0,
                employee_type="hourly",
                is_active=True,
            )
            s.add_all([reg_emp, hr_emp])
            s.flush()
            # 8 月：regular gross=42000 (含 ot 2000 + supdiv 3000) → base=37000
            # 8 月：hourly gross=15000 (含 ot 500 + supdiv 0) → art=14500
            s.add_all(
                [
                    SalaryRecord(
                        employee_id=reg_emp.id,
                        salary_year=2026,
                        salary_month=8,
                        gross_salary=42000,
                        overtime_pay=2000,
                        supervisor_dividend=3000,
                        festival_bonus=1000,
                        labor_insurance_employer=2500,
                        net_salary=30000,
                        total_deduction=12000,
                    ),
                    SalaryRecord(
                        employee_id=hr_emp.id,
                        salary_year=2026,
                        salary_month=8,
                        gross_salary=15000,
                        overtime_pay=500,
                        supervisor_dividend=0,
                        festival_bonus=500,
                        labor_insurance_employer=800,
                        net_salary=14000,
                        total_deduction=1000,
                    ),
                ]
            )
            s.commit()
        with sf() as s:
            data = build_monthly_pnl(s, 2026)
        rows = data["sections"]
        # 8 月 index = 7
        assert (
            _row(rows, "personnel_expense", "personnel_base_salary")["monthly"][7]
            == 37000
        )
        assert (
            _row(rows, "personnel_expense", "personnel_art_teacher_hourly")["monthly"][
                7
            ]
            == 14500
        )
        # festival 跨 role 加總 = 1000 + 500 = 1500
        assert (
            _row(rows, "personnel_expense", "personnel_festival_bonus")["monthly"][7]
            == 1500
        )
        # 勞保（雇主）跨 role 加總 = 2500 + 800 = 3300
        assert (
            _row(rows, "personnel_expense", "personnel_labor_insurance")["monthly"][7]
            == 3300
        )

    def test_aggregator_fixed_costs_in_variable_section(self, pnl_client):
        """7 條變動支出固定費用從 monthly_fixed_costs 取，row key 一對一映射。"""
        from models.monthly_fixed_cost import MonthlyFixedCost

        _, sf = pnl_client
        with sf() as s:
            s.add_all(
                [
                    MonthlyFixedCost(
                        year=2026, month=3, category="rent", amount=500000
                    ),
                    MonthlyFixedCost(year=2026, month=3, category="water", amount=5989),
                    MonthlyFixedCost(
                        year=2026, month=3, category="electricity", amount=16525
                    ),
                    MonthlyFixedCost(year=2026, month=3, category="phone", amount=1273),
                    MonthlyFixedCost(
                        year=2026, month=3, category="office_petty_cash", amount=18134
                    ),
                    MonthlyFixedCost(
                        year=2026, month=3, category="kitchen_petty_cash", amount=19202
                    ),
                    MonthlyFixedCost(
                        year=2026, month=3, category="meals", amount=50000
                    ),
                    # 另一年的條目不應出現
                    MonthlyFixedCost(
                        year=2025, month=3, category="rent", amount=400000
                    ),
                ]
            )
            s.commit()
        with sf() as s:
            data = build_monthly_pnl(s, 2026)
        rows = data["sections"]
        # 3 月 index = 2
        assert _row(rows, "variable_expense", "variable_rent")["monthly"][2] == 500000
        assert _row(rows, "variable_expense", "variable_water")["monthly"][2] == 5989
        assert (
            _row(rows, "variable_expense", "variable_electricity")["monthly"][2]
            == 16525
        )
        assert _row(rows, "variable_expense", "variable_phone")["monthly"][2] == 1273
        assert (
            _row(rows, "variable_expense", "variable_office_petty_cash")["monthly"][2]
            == 18134
        )
        assert (
            _row(rows, "variable_expense", "variable_kitchen_petty_cash")["monthly"][2]
            == 19202
        )
        assert _row(rows, "variable_expense", "variable_meals")["monthly"][2] == 50000
        # subtotal = 7 條固定 + vendor(0)
        subtotal_3 = _row(rows, "variable_expense", "variable_subtotal")["monthly"][2]
        assert subtotal_3 == 500000 + 5989 + 16525 + 1273 + 18134 + 19202 + 50000

    def test_aggregator_old_pension_reserve_in_personnel(self, pnl_client):
        """old_pension_reserve 從 monthly_fixed_costs 讀但落在 personnel section
        而非 variable section（屬勞退非變動）。"""
        from models.monthly_fixed_cost import MonthlyFixedCost

        _, sf = pnl_client
        with sf() as s:
            s.add(
                MonthlyFixedCost(
                    year=2026, month=4, category="old_pension_reserve", amount=10000
                )
            )
            s.commit()
        with sf() as s:
            data = build_monthly_pnl(s, 2026)
        rows = data["sections"]
        # 4 月 index = 3
        assert (
            _row(rows, "personnel_expense", "personnel_old_pension_reserve")["monthly"][
                3
            ]
            == 10000
        )
        # personnel_subtotal 涵蓋 10000（其他列為 0）
        assert (
            _row(rows, "personnel_expense", "personnel_subtotal")["monthly"][3] == 10000
        )
        # 不出現在 variable section（key 名不衝突，但驗證概念）
        variable_keys = {
            r["key"]
            for r in next(s for s in rows if s["key"] == "variable_expense")["rows"]
        }
        assert "old_pension_reserve" not in variable_keys
        assert "personnel_old_pension_reserve" not in variable_keys


# ── 端點整合測試 ───────────────────────────────────────────────────────────


class TestMonthlyPnLEndpoint:
    def test_endpoint_requires_reports_permission(self, pnl_client):
        """未登入應 401/403。"""
        client, _ = pnl_client
        r = client.get("/api/reports/monthly-pnl?year=2026")
        assert r.status_code in (401, 403)

    def test_endpoint_response_shape(self, pnl_client):
        """登入後回傳 200，且結構含 year/sections/totals/pending_items；4 section + 12 月長度。"""
        client, sf = pnl_client
        _seed_admin(sf, client)
        r = client.get("/api/reports/monthly-pnl?year=2026")
        assert r.status_code == 200
        body = r.json()
        assert body["year"] == 2026
        assert "sections" in body
        assert "totals" in body
        assert "pending_items" in body

        section_keys = [s["key"] for s in body["sections"]]
        assert section_keys == [
            "stats",
            "income",
            "personnel_expense",
            "variable_expense",
        ]
        # 每個 amount row monthly 長度 12
        for sec in body["sections"]:
            for row in sec["rows"]:
                assert len(row["monthly"]) == 12

        # totals 四個 key
        assert set(body["totals"].keys()) == {
            "income_total",
            "refund_total",
            "expense_total",
            "net_cashflow",
        }
        for k, v in body["totals"].items():
            assert len(v["monthly"]) == 12
            assert isinstance(v["total"], int)
