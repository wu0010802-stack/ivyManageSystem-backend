"""驗 GET /api/salaries/{record_id}/unused-leave-payout-detail endpoint。

五個測試案例：
1. HR (SALARY_READ + admin/hr role) 看任何員工 → 200 + logs
2. 員工本人（supervisor role + SALARY_READ + linked employee_id）看自己 → 200
3. 員工查他人 → 403
4. SalaryRecord 不存在 → 404
5. SalaryRecord 存在但無 logs → 200 + logs=[]
"""

import os
import sys
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import api.salary as salary_module

# 必須在 Base.metadata.create_all 之前 import，讓 ORM 知道此 table
from models.unused_leave_payout_log import UnusedLeavePayoutLog  # noqa: F401

from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.salary import router as salary_router
from models.database import Base, Employee, User, SalaryRecord
from utils.auth import hash_password

# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def payout_client(tmp_path):
    """隔離 sqlite 測試環境（薪資 payout-detail 端點用）。"""
    db_path = tmp_path / "salary-payout-detail.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    salary_module.init_salary_services(MagicMock(), MagicMock())

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


# ── seed helpers ───────────────────────────────────────────────────────────────


def _create_employee(
    session, *, employee_id="E001", name="測試員工", base_salary=40000
):
    emp = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=base_salary,
        employee_type="regular",
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _create_salary_record(session, employee_id, *, year=2026, month=4):
    record = SalaryRecord(
        employee_id=employee_id,
        salary_year=year,
        salary_month=month,
        base_salary=40000,
        gross_salary=40000,
        total_deduction=0,
        net_salary=40000,
        unused_leave_payout=Decimal("1200.00"),
        is_finalized=False,
    )
    session.add(record)
    session.flush()
    return record


def _create_payout_log(
    session, *, employee_id, salary_record_id, source_type="comp_grant_expiry"
):
    from datetime import date

    log = UnusedLeavePayoutLog(
        employee_id=employee_id,
        source_type=source_type,
        source_ref_id=None,
        hours=8.0,
        hourly_wage=Decimal("150.00"),
        amount=Decimal("1200.00"),
        wage_basis_date=date(2026, 4, 1),
        salary_record_id=salary_record_id,
        salary_period_year=2026,
        salary_period_month=4,
        meta={"note": "到期補休"},
    )
    session.add(log)
    session.flush()
    return log


def _create_user(
    session,
    *,
    username,
    password,
    role,
    permission_names,
    employee_id=None,
):
    user = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permission_names=(
            permission_names
            if isinstance(permission_names, list)
            else [permission_names]
        ),
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password):
    res = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert res.status_code == 200, f"Login failed: {res.text}"


# ── test cases ─────────────────────────────────────────────────────────────────


class TestUnusedLeavePayoutDetailEndpoint:
    def test_hr_can_see_any_employee_payout_detail(self, payout_client):
        """HR (SALARY_READ + hr role) 看任何員工 → 200 + logs"""
        client, sf = payout_client
        with sf() as session:
            emp = _create_employee(session)
            record = _create_salary_record(session, emp.id)
            _create_payout_log(session, employee_id=emp.id, salary_record_id=record.id)
            _create_user(
                session,
                username="hr_user",
                password="HrPass123",
                role="hr",
                permission_names=["SALARY_READ"],
            )
            record_id = record.id
            emp_id = emp.id
            session.commit()

        _login(client, "hr_user", "HrPass123")
        res = client.get(f"/api/salaries/{record_id}/unused-leave-payout-detail")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["salary_record_id"] == record_id
        assert body["employee_id"] == emp_id
        assert body["total_amount"] == 1200.0
        assert len(body["logs"]) == 1
        log = body["logs"][0]
        assert log["source_type"] == "comp_grant_expiry"
        assert log["hours"] == 8.0
        assert log["hourly_wage"] == 150.0
        assert log["amount"] == 1200.0
        assert log["wage_basis_date"] == "2026-04-01"
        assert log["meta"] == {"note": "到期補休"}

    def test_employee_can_see_self_payout_detail(self, payout_client):
        """員工本人（supervisor role + SALARY_READ + linked employee_id）看自己 → 200"""
        client, sf = payout_client
        with sf() as session:
            emp = _create_employee(session, employee_id="E002", name="自查員工")
            record = _create_salary_record(session, emp.id)
            _create_payout_log(session, employee_id=emp.id, salary_record_id=record.id)
            _create_user(
                session,
                username="self_user",
                password="SelfPass123",
                role="supervisor",
                permission_names=["SALARY_READ"],
                employee_id=emp.id,
            )
            record_id = record.id
            emp_id = emp.id
            session.commit()

        _login(client, "self_user", "SelfPass123")
        res = client.get(f"/api/salaries/{record_id}/unused-leave-payout-detail")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["salary_record_id"] == record_id
        assert body["employee_id"] == emp_id
        assert len(body["logs"]) == 1

    def test_employee_cannot_see_others(self, payout_client):
        """員工查他人 → 403"""
        client, sf = payout_client
        with sf() as session:
            emp_target = _create_employee(session, employee_id="E003", name="目標員工")
            emp_other = _create_employee(session, employee_id="E004", name="他人員工")
            record = _create_salary_record(session, emp_target.id)
            # 建立 other_user，綁到 emp_other（不是 emp_target）
            _create_user(
                session,
                username="other_user",
                password="OtherPass123",
                role="supervisor",
                permission_names=["SALARY_READ"],
                employee_id=emp_other.id,
            )
            record_id = record.id
            session.commit()

        _login(client, "other_user", "OtherPass123")
        res = client.get(f"/api/salaries/{record_id}/unused-leave-payout-detail")
        assert res.status_code == 403, res.text

    def test_404_when_salary_record_not_found(self, payout_client):
        """SalaryRecord 不存在 → 404"""
        client, sf = payout_client
        with sf() as session:
            _create_user(
                session,
                username="hr_404",
                password="Hr404Pass123",
                role="hr",
                permission_names=["SALARY_READ"],
            )
            session.commit()

        _login(client, "hr_404", "Hr404Pass123")
        res = client.get("/api/salaries/99999/unused-leave-payout-detail")
        assert res.status_code == 404, res.text

    def test_empty_logs_when_no_payout(self, payout_client):
        """SalaryRecord 存在但無 logs → 200 + logs=[]"""
        client, sf = payout_client
        with sf() as session:
            emp = _create_employee(session, employee_id="E005", name="空 log 員工")
            record = _create_salary_record(session, emp.id)
            # 不建立任何 UnusedLeavePayoutLog
            _create_user(
                session,
                username="hr_empty",
                password="HrEmpty123",
                role="hr",
                permission_names=["SALARY_READ"],
            )
            record_id = record.id
            emp_id = emp.id
            session.commit()

        _login(client, "hr_empty", "HrEmpty123")
        res = client.get(f"/api/salaries/{record_id}/unused-leave-payout-detail")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["salary_record_id"] == record_id
        assert body["employee_id"] == emp_id
        assert body["logs"] == []
        assert body["total_amount"] == 1200.0
