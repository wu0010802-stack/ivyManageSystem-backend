"""finance_report_service 與 /api/reports/finance-summary 端點測試。"""

import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.reports import router as reports_router
from models.database import (
    Base,
    Employee,
    User,
    ActivityPaymentRecord,
    SalaryRecord,
)
from models.fees import StudentFeeRecord, StudentFeeRefund
from services import finance_report_service as svc
from utils.auth import hash_password


@pytest.fixture
def fin_client(tmp_path):
    db_path = tmp_path / "fin.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
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
                username="fin_admin",
                password_hash=hash_password("FinPass123"),
                role="admin",
                permissions=-1,
                is_active=True,
                must_change_password=False,
            )
        )
        s.commit()
    r = client.post(
        "/api/auth/login", json={"username": "fin_admin", "password": "FinPass123"}
    )
    assert r.status_code == 200


# ── Provider 單元測試 ──────────────────────────────────────────────────────


class TestTuitionProvider:
    def test_only_paid_counted(self, fin_client):
        _, sf = fin_client
        with sf() as s:
            s.add_all(
                [
                    StudentFeeRecord(
                        student_id=1,
                        fee_item_id=1,
                        period="2026-1",
                        amount_due=5000,
                        amount_paid=5000,
                        status="paid",
                        payment_date=date(2026, 3, 10),
                        student_name="A",
                        fee_item_name="月費",
                    ),
                    StudentFeeRecord(
                        student_id=2,
                        fee_item_id=1,
                        period="2026-1",
                        amount_due=5000,
                        amount_paid=0,
                        status="unpaid",
                        payment_date=None,
                        student_name="B",
                        fee_item_name="月費",
                    ),
                ]
            )
            s.commit()
        with sf() as s:
            out = svc.get_tuition_revenue_by_month(s, 2026)
        assert out == {3: 5000}

    def test_refund_by_month(self, fin_client):
        _, sf = fin_client
        with sf() as s:
            s.add(
                StudentFeeRefund(
                    record_id=1,
                    amount=1000,
                    reason="退學",
                    refunded_at=datetime(2026, 4, 5, 10, 0),
                    refunded_by="admin",
                    idempotency_key="k1",
                )
            )
            s.commit()
        with sf() as s:
            out = svc.get_tuition_refund_by_month(s, 2026)
        assert out == {4: 1000}


class TestActivityProvider:
    def test_payment_and_refund_split(self, fin_client):
        _, sf = fin_client
        with sf() as s:
            s.add_all(
                [
                    ActivityPaymentRecord(
                        registration_id=1,
                        type="payment",
                        amount=2000,
                        payment_date=date(2026, 3, 10),
                    ),
                    ActivityPaymentRecord(
                        registration_id=1,
                        type="refund",
                        amount=500,
                        payment_date=date(2026, 4, 2),
                    ),
                ]
            )
            s.commit()
        with sf() as s:
            rev = svc.get_activity_revenue_by_month(s, 2026)
            ref = svc.get_activity_refund_by_month(s, 2026)
        assert rev == {3: 2000}
        assert ref == {4: 500}


class TestSalaryExpense:
    def test_gross_plus_employer_split(self, fin_client):
        _, sf = fin_client
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
                    salary_month=3,
                    gross_salary=30000,
                    labor_insurance_employer=2500,
                    health_insurance_employer=1400,
                    pension_employer=1800,
                    net_salary=25000,
                    total_deduction=5000,
                )
            )
            s.commit()
        with sf() as s:
            out = svc.get_salary_expense_by_month(s, 2026)
        assert out == {3: {"employee_gross": 30000, "employer_benefit": 5700}}


# ── Aggregator ─────────────────────────────────────────────────────────────


class TestAggregator:
    def test_full_year_contains_12_months(self, fin_client):
        _, sf = fin_client
        with sf() as s:
            data = svc.build_finance_summary(s, 2026)
        assert len(data["monthly_trend"]) == 12
        assert data["period"] == {"year": 2026, "month": None}
        assert data["summary"]["total_revenue"] == 0
        assert data["summary"]["net_cashflow"] == 0

    def test_single_month_contains_1_row(self, fin_client):
        _, sf = fin_client
        with sf() as s:
            data = svc.build_finance_summary(s, 2026, month=3)
        assert len(data["monthly_trend"]) == 1
        assert data["monthly_trend"][0]["month"] == 3

    def test_net_cashflow_accounts_refund(self, fin_client):
        _, sf = fin_client
        with sf() as s:
            s.add_all(
                [
                    StudentFeeRecord(
                        student_id=1,
                        fee_item_id=1,
                        period="2026-1",
                        amount_due=10000,
                        amount_paid=10000,
                        status="paid",
                        payment_date=date(2026, 3, 10),
                        student_name="A",
                        fee_item_name="月費",
                    ),
                    StudentFeeRefund(
                        record_id=1,
                        amount=2000,
                        reason="x",
                        refunded_at=datetime(2026, 3, 15, 9, 0),
                        refunded_by="admin",
                        idempotency_key="r1",
                    ),
                ]
            )
            s.commit()
        with sf() as s:
            data = svc.build_finance_summary(s, 2026, month=3)
        row = data["monthly_trend"][0]
        assert row["revenue"] == 10000
        assert row["refund"] == 2000
        assert row["net"] == 8000

    def test_categories_have_expected_shape(self, fin_client):
        _, sf = fin_client
        with sf() as s:
            data = svc.build_finance_summary(s, 2026)
        rev_cats = {c["category"] for c in data["revenue_by_category"]}
        exp_cats = {c["category"] for c in data["expense_by_category"]}
        assert rev_cats == {"tuition", "activity"}
        assert exp_cats == {"salary_gross", "employer_benefit"}

    def test_cross_year_isolation(self, fin_client):
        _, sf = fin_client
        with sf() as s:
            s.add(
                StudentFeeRecord(
                    student_id=1,
                    fee_item_id=1,
                    period="2025-2",
                    amount_due=5000,
                    amount_paid=5000,
                    status="paid",
                    payment_date=date(2025, 12, 10),
                    student_name="A",
                    fee_item_name="月費",
                )
            )
            s.commit()
        with sf() as s:
            assert svc.get_tuition_revenue_by_month(s, 2026) == {}
            assert svc.get_tuition_revenue_by_month(s, 2025) == {12: 5000}


# ── 端點整合測試 ───────────────────────────────────────────────────────────


class TestFinanceSummaryEndpoint:
    def test_year_only(self, fin_client):
        client, sf = fin_client
        _seed_admin(sf, client)
        r = client.get("/api/reports/finance-summary?year=2026")
        assert r.status_code == 200
        body = r.json()
        assert body["period"]["year"] == 2026
        assert body["period"]["month"] is None
        assert len(body["monthly_trend"]) == 12
        assert "revenue_by_category" in body
        assert "expense_by_category" in body

    def test_with_month(self, fin_client):
        client, sf = fin_client
        _seed_admin(sf, client)
        r = client.get("/api/reports/finance-summary?year=2026&month=3")
        assert r.status_code == 200
        body = r.json()
        assert body["period"]["month"] == 3
        assert len(body["monthly_trend"]) == 1
        assert body["monthly_trend"][0]["month"] == 3

    def test_requires_auth(self, fin_client):
        client, _ = fin_client
        r = client.get("/api/reports/finance-summary?year=2026")
        assert r.status_code in (401, 403)

    def test_year_out_of_range_rejected(self, fin_client):
        client, sf = fin_client
        _seed_admin(sf, client)
        r = client.get("/api/reports/finance-summary?year=1999")
        assert r.status_code == 422

    def test_with_actual_data(self, fin_client):
        client, sf = fin_client
        _seed_admin(sf, client)
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
            s.add_all(
                [
                    StudentFeeRecord(
                        student_id=1,
                        fee_item_id=1,
                        period="2026-1",
                        amount_due=5000,
                        amount_paid=5000,
                        status="paid",
                        payment_date=date(2026, 4, 5),
                        student_name="A",
                        fee_item_name="月費",
                    ),
                    ActivityPaymentRecord(
                        registration_id=1,
                        type="payment",
                        amount=1200,
                        payment_date=date(2026, 4, 6),
                    ),
                    SalaryRecord(
                        employee_id=emp.id,
                        salary_year=2026,
                        salary_month=4,
                        gross_salary=30000,
                        labor_insurance_employer=2500,
                        health_insurance_employer=1400,
                        pension_employer=1800,
                        net_salary=25000,
                        total_deduction=5000,
                    ),
                ]
            )
            s.commit()
        r = client.get("/api/reports/finance-summary?year=2026&month=4")
        assert r.status_code == 200
        body = r.json()
        row = body["monthly_trend"][0]
        assert row["revenue"] == 6200  # 5000 + 1200
        assert row["refund"] == 0
        assert row["expense"] == 35700  # 30000 + 5700
        assert row["net"] == 6200 - 35700


# ── 快取行為 ───────────────────────────────────────────────────────────────


class TestSummaryCaching:
    def test_second_call_serves_from_cache(self, fin_client):
        """第二次呼叫應從 ReportSnapshot 讀，不重跑 builder。"""
        client, sf = fin_client
        _seed_admin(sf, client)
        r1 = client.get("/api/reports/finance-summary?year=2026").json()
        # 在快取期間新增一筆繳費記錄
        with sf() as s:
            s.add(
                StudentFeeRecord(
                    student_id=1,
                    fee_item_id=1,
                    period="2026-1",
                    amount_due=9999,
                    amount_paid=9999,
                    status="paid",
                    payment_date=date(2026, 3, 1),
                    student_name="Z",
                    fee_item_name="月費",
                )
            )
            s.commit()
        r2 = client.get("/api/reports/finance-summary?year=2026").json()
        # cache 命中 → 金額與第一次一致（未看到新資料）
        assert r1["summary"]["total_revenue"] == r2["summary"]["total_revenue"]

    def test_different_params_have_independent_cache(self, fin_client):
        client, sf = fin_client
        _seed_admin(sf, client)
        r1 = client.get("/api/reports/finance-summary?year=2026").json()
        r2 = client.get("/api/reports/finance-summary?year=2026&month=3").json()
        assert len(r1["monthly_trend"]) == 12
        assert len(r2["monthly_trend"]) == 1


# ── Detail 端點 ────────────────────────────────────────────────────────────


class TestDetailEndpoint:
    def test_empty(self, fin_client):
        client, sf = fin_client
        _seed_admin(sf, client)
        r = client.get("/api/reports/finance-summary/detail?year=2026&month=3")
        assert r.status_code == 200
        body = r.json()
        assert body["tuition"] == []
        assert body["activity"] == []
        assert body["salary"] == []
        assert body["period"] == {"year": 2026, "month": 3}

    def test_includes_tuition_activity_salary(self, fin_client):
        client, sf = fin_client
        _seed_admin(sf, client)
        with sf() as s:
            emp = Employee(
                employee_id="E1",
                name="Teacher",
                base_salary=30000,
                employee_type="regular",
                is_active=True,
            )
            s.add(emp)
            s.flush()
            s.add_all(
                [
                    StudentFeeRecord(
                        student_id=1,
                        fee_item_id=1,
                        period="2026-1",
                        amount_due=5000,
                        amount_paid=5000,
                        status="paid",
                        payment_date=date(2026, 3, 5),
                        student_name="Alice",
                        classroom_name="大班",
                        fee_item_name="月費",
                        payment_method="現金",
                    ),
                    StudentFeeRefund(
                        record_id=1,
                        amount=500,
                        reason="請假退款",
                        refunded_at=datetime(2026, 3, 10, 10, 0),
                        refunded_by="admin",
                        idempotency_key="refund1",
                    ),
                    ActivityPaymentRecord(
                        registration_id=1,
                        type="payment",
                        amount=1500,
                        payment_date=date(2026, 3, 6),
                        payment_method="轉帳",
                        operator="staff",
                    ),
                    SalaryRecord(
                        employee_id=emp.id,
                        salary_year=2026,
                        salary_month=3,
                        gross_salary=30000,
                        labor_insurance_employer=2500,
                        health_insurance_employer=1400,
                        pension_employer=1800,
                        net_salary=25000,
                        total_deduction=5000,
                    ),
                ]
            )
            s.commit()
        r = client.get("/api/reports/finance-summary/detail?year=2026&month=3").json()
        assert len(r["tuition"]) == 2  # 1 繳費 + 1 退款
        assert {x["kind"] for x in r["tuition"]} == {"payment", "refund"}
        assert len(r["activity"]) == 1
        assert r["activity"][0]["kind"] == "payment"
        assert len(r["salary"]) == 1
        salary_row = r["salary"][0]
        assert salary_row["employee_name"] == "Teacher"
        assert salary_row["gross_salary"] == 30000
        assert salary_row["employer_benefit"] == 5700
        assert salary_row["real_cost"] == 35700


# ── Export 端點 ────────────────────────────────────────────────────────────


class TestExportEndpoint:
    def test_year_only_returns_xlsx(self, fin_client):
        client, sf = fin_client
        _seed_admin(sf, client)
        r = client.get("/api/reports/finance-summary/export?year=2026")
        assert r.status_code == 200
        assert (
            r.headers["content-type"]
            == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert r.content[:2] == b"PK"  # xlsx 是 zip，以 PK 開頭

    def test_with_month_adds_detail_sheets(self, fin_client):
        from io import BytesIO
        from openpyxl import load_workbook

        client, sf = fin_client
        _seed_admin(sf, client)
        r = client.get("/api/reports/finance-summary/export?year=2026&month=3")
        assert r.status_code == 200
        wb = load_workbook(BytesIO(r.content))
        assert "月度彙總" in wb.sheetnames
        assert "分類統計" in wb.sheetnames
        assert "學費明細" in wb.sheetnames
        assert "才藝明細" in wb.sheetnames
        assert "薪資明細" in wb.sheetnames
