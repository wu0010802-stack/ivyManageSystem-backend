"""
Portal 薪資預覽 /salary-preview 端點測試

驗證：
1. SalaryRecord 存在且 finalized 時，response 含 unused_leave_payout
2. SalaryRecord 不存在時，回應不含 salary 欄位
3. SalaryRecord 存在但未 finalized 時，salary_status='draft' 且不含詳細金額
"""

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.portal import router as portal_router
from models.database import (
    Employee,
    User,
    SalaryRecord,
)
from utils.auth import create_access_token, hash_password


@pytest.fixture
def portal_client(test_db_session):
    """建立隔離的測試 app（portal 薪資預覽用）。"""
    session = test_db_session

    app = FastAPI()
    app.include_router(portal_router)

    with TestClient(app) as client:
        yield client, session


def test_salary_preview_returns_unused_leave_payout_when_finalized(
    portal_client,
):
    """SalaryRecord 存在且 finalized 時，response 含 unused_leave_payout"""
    client, session = portal_client

    try:
        # 建立員工
        emp = Employee(
            name="教師一",
            employee_id="E001",
            base_salary=30000,
            hire_date=date(2020, 1, 1),
            is_active=True,
        )
        session.add(emp)
        session.flush()

        # 建立登入使用者
        user = User(
            employee_id=emp.id,
            username="teacher1",
            password_hash=hash_password("password"),
            role="teacher",
        )
        session.add(user)
        session.flush()

        # 建立 SalaryRecord（finalized、含 unused_leave_payout）
        salary = SalaryRecord(
            employee_id=emp.id,
            salary_year=2026,
            salary_month=5,
            base_salary=Decimal("30000"),
            festival_bonus=Decimal("0"),
            overtime_bonus=Decimal("0"),
            performance_bonus=Decimal("0"),
            special_bonus=Decimal("0"),
            supervisor_dividend=Decimal("0"),
            overtime_pay=Decimal("0"),
            meeting_overtime_pay=Decimal("0"),
            labor_insurance_employee=Decimal("500"),
            health_insurance_employee=Decimal("400"),
            supplementary_health_employee=Decimal("0"),
            pension_employee=Decimal("1500"),
            late_deduction=Decimal("0"),
            early_leave_deduction=Decimal("0"),
            missing_punch_deduction=Decimal("0"),
            leave_deduction=Decimal("0"),
            meeting_absence_deduction=Decimal("0"),
            other_deduction=Decimal("0"),
            gross_salary=Decimal("30000"),
            total_deduction=Decimal("2400"),
            net_salary=Decimal("27600"),
            unused_leave_payout=Decimal("1000"),  # 特休未休折現
            is_finalized=True,
            needs_recalc=False,
            version=1,
        )
        session.add(salary)
        session.commit()

        # 生成 token
        token = create_access_token(
            data={"username": user.username, "employee_id": emp.id}
        )

        # 呼叫 /api/portal/salary-preview
        response = client.get(
            "/api/portal/salary-preview?year=2026&month=5",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()

        assert data["year"] == 2026
        assert data["month"] == 5
        assert data["salary_status"] == "finalized"
        assert data["salary"] is not None
        assert data["salary"]["unused_leave_payout"] == 1000.0
        assert data["salary"]["base_salary"] == 30000

    finally:
        session.close()


def test_salary_preview_returns_zero_when_no_salary_record(portal_client):
    """SalaryRecord 不存在 → salary_status='none' 且不含詳細金額"""
    client, session = portal_client

    try:
        # 建立員工
        emp = Employee(
            name="教師二",
            employee_id="E002",
            base_salary=35000,
            hire_date=date(2020, 1, 1),
            is_active=True,
        )
        session.add(emp)
        session.flush()

        # 建立登入使用者
        user = User(
            employee_id=emp.id,
            username="teacher2",
            password_hash=hash_password("password"),
            role="teacher",
        )
        session.add(user)
        session.commit()

        # 生成 token
        token = create_access_token(
            data={"username": user.username, "employee_id": emp.id}
        )

        # 呼叫 /api/portal/salary-preview（該月無薪資紀錄）
        response = client.get(
            "/api/portal/salary-preview?year=2026&month=5",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()

        assert data["year"] == 2026
        assert data["month"] == 5
        assert data["salary_status"] == "none"
        assert data["salary"] is None

    finally:
        session.close()


def test_salary_preview_returns_zero_payout_when_null(portal_client):
    """SalaryRecord 存在但 unused_leave_payout=NULL → 0.0"""
    client, session = portal_client

    try:
        # 建立員工
        emp = Employee(
            name="教師三",
            employee_id="E003",
            base_salary=32000,
            hire_date=date(2020, 1, 1),
            is_active=True,
        )
        session.add(emp)
        session.flush()

        # 建立登入使用者
        user = User(
            employee_id=emp.id,
            username="teacher3",
            password_hash=hash_password("password"),
            role="teacher",
        )
        session.add(user)
        session.flush()

        # 建立 SalaryRecord（finalized、unused_leave_payout=NULL）
        salary = SalaryRecord(
            employee_id=emp.id,
            salary_year=2026,
            salary_month=4,
            base_salary=Decimal("32000"),
            festival_bonus=Decimal("0"),
            overtime_bonus=Decimal("0"),
            performance_bonus=Decimal("0"),
            special_bonus=Decimal("0"),
            supervisor_dividend=Decimal("0"),
            overtime_pay=Decimal("0"),
            meeting_overtime_pay=Decimal("0"),
            labor_insurance_employee=Decimal("500"),
            health_insurance_employee=Decimal("400"),
            supplementary_health_employee=Decimal("0"),
            pension_employee=Decimal("1600"),
            late_deduction=Decimal("0"),
            early_leave_deduction=Decimal("0"),
            missing_punch_deduction=Decimal("0"),
            leave_deduction=Decimal("0"),
            meeting_absence_deduction=Decimal("0"),
            other_deduction=Decimal("0"),
            gross_salary=Decimal("32000"),
            total_deduction=Decimal("2500"),
            net_salary=Decimal("29500"),
            unused_leave_payout=None,  # NULL
            is_finalized=True,
            needs_recalc=False,
            version=1,
        )
        session.add(salary)
        session.commit()

        # 生成 token
        token = create_access_token(
            data={"username": user.username, "employee_id": emp.id}
        )

        # 呼叫 /api/portal/salary-preview
        response = client.get(
            "/api/portal/salary-preview?year=2026&month=4",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()

        assert data["year"] == 2026
        assert data["month"] == 4
        assert data["salary_status"] == "finalized"
        assert data["salary"] is not None
        assert data["salary"]["unused_leave_payout"] == 0.0

    finally:
        session.close()


def test_salary_preview_excludes_salary_when_draft(portal_client):
    """SalaryRecord 存在但未 finalized → salary_status='draft' 且不含詳細金額"""
    client, session = portal_client

    try:
        # 建立員工
        emp = Employee(
            name="教師四",
            employee_id="E004",
            base_salary=31000,
            hire_date=date(2020, 1, 1),
            is_active=True,
        )
        session.add(emp)
        session.flush()

        # 建立登入使用者
        user = User(
            employee_id=emp.id,
            username="teacher4",
            password_hash=hash_password("password"),
            role="teacher",
        )
        session.add(user)
        session.flush()

        # 建立 SalaryRecord（NOT finalized）
        salary = SalaryRecord(
            employee_id=emp.id,
            salary_year=2026,
            salary_month=6,
            base_salary=Decimal("31000"),
            festival_bonus=Decimal("0"),
            overtime_bonus=Decimal("0"),
            performance_bonus=Decimal("0"),
            special_bonus=Decimal("0"),
            supervisor_dividend=Decimal("0"),
            overtime_pay=Decimal("0"),
            meeting_overtime_pay=Decimal("0"),
            labor_insurance_employee=Decimal("500"),
            health_insurance_employee=Decimal("400"),
            supplementary_health_employee=Decimal("0"),
            pension_employee=Decimal("1550"),
            late_deduction=Decimal("0"),
            early_leave_deduction=Decimal("0"),
            missing_punch_deduction=Decimal("0"),
            leave_deduction=Decimal("0"),
            meeting_absence_deduction=Decimal("0"),
            other_deduction=Decimal("0"),
            gross_salary=Decimal("31000"),
            total_deduction=Decimal("2450"),
            net_salary=Decimal("28550"),
            unused_leave_payout=Decimal("800"),
            is_finalized=False,  # NOT finalized
            needs_recalc=False,
            version=1,
        )
        session.add(salary)
        session.commit()

        # 生成 token
        token = create_access_token(
            data={"username": user.username, "employee_id": emp.id}
        )

        # 呼叫 /api/portal/salary-preview
        response = client.get(
            "/api/portal/salary-preview?year=2026&month=6",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()

        assert data["year"] == 2026
        assert data["month"] == 6
        assert data["salary_status"] == "draft"
        assert data["salary"] is None  # 未定案時不 expose 金額

    finally:
        session.close()
