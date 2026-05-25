"""Pydantic validator 回歸測試：leave_hours<8 → start_time/end_time 必填。

Task 1 of employee-leave-attendance-sync:
  - LeaveCreate validator: POST /api/leaves, leave_hours=4 缺 start_time → 422
  - LeaveCreate validator: POST /api/leaves, leave_hours=8 不傳 start_time → 201
  - LeaveUpdate validator: PUT /api/leaves/{id}, leave_hours=4 缺 start_time → 422
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.leaves as leaves_module
import models.base as base_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from models.database import Base, Employee, LeaveRecord, User
from utils.auth import hash_password


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """SQLite in-memory + TestClient + mocked salary engine。"""
    db_path = tmp_path / "partial-validator.sqlite"
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

    fake_salary_engine = MagicMock()
    monkeypatch.setattr(leaves_module, "_salary_engine", fake_salary_engine)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(leaves_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _setup_admin_and_employee(session_factory):
    """建立一個 admin User（無 employee_id，避免自我核准守衛）與一個員工，回傳員工 id。"""
    with session_factory() as session:
        emp = Employee(
            employee_id="V001",
            name="測試員工",
            base_salary=36000,
            is_active=True,
        )
        session.add(emp)
        session.flush()
        emp_id = emp.id

        user = User(
            employee_id=None,
            username="v_admin",
            password_hash=hash_password("VAdmin123"),
            role="admin",
            permissions=-1,
            is_active=True,
            must_change_password=False,
        )
        session.add(user)
        session.commit()

    return emp_id


def _setup_leave(session_factory, employee_id: int) -> int:
    """直接用 ORM 建一筆待審 full-day 假單，回傳 leave id。"""
    with session_factory() as session:
        lv = LeaveRecord(
            employee_id=employee_id,
            leave_type="personal",
            start_date=date(2026, 5, 22),
            end_date=date(2026, 5, 22),
            leave_hours=8.0,
            start_time=None,
            end_time=None,
            is_approved=None,
        )
        session.add(lv)
        session.commit()
        return lv.id


def _login(client: TestClient):
    resp = client.post(
        "/api/auth/login", json={"username": "v_admin", "password": "VAdmin123"}
    )
    assert resp.status_code == 200, f"login failed: {resp.json()}"


# ─────────────────────────────────────────────────────────────────────────
# 測試案例
# ─────────────────────────────────────────────────────────────────────────


def test_leave_create_partial_hours_requires_start_end_time(app_client):
    """leave_hours<8 但缺 start_time 應 422。"""
    client, session_factory = app_client
    emp_id = _setup_admin_and_employee(session_factory)
    _login(client)

    resp = client.post(
        "/api/leaves",
        json={
            "employee_id": emp_id,
            "leave_type": "personal",
            "start_date": "2026-05-22",
            "end_date": "2026-05-22",
            "leave_hours": 4,
            # 故意不傳 start_time / end_time
            "reason": "test partial",
        },
    )
    assert (
        resp.status_code == 422
    ), f"缺 start_time 的部分請假應 422，實際 status={resp.status_code}, body={resp.json()}"
    detail = resp.json().get("detail", "")
    assert (
        "start_time" in str(detail).lower() or "end_time" in str(detail).lower()
    ), f"422 detail 應提到 start_time/end_time，實際 detail={detail}"


def test_leave_create_full_day_no_time_required(app_client):
    """全天請假(leave_hours=8)不要 start_time，應 201。"""
    client, session_factory = app_client
    emp_id = _setup_admin_and_employee(session_factory)
    _login(client)

    resp = client.post(
        "/api/leaves",
        json={
            "employee_id": emp_id,
            "leave_type": "personal",
            "start_date": "2026-05-22",
            "end_date": "2026-05-22",
            "leave_hours": 8,
            "reason": "test full day",
        },
    )
    assert resp.status_code in (
        200,
        201,
    ), f"全天請假不傳 start_time 應 200/201，實際 status={resp.status_code}, body={resp.json()}"


def test_leave_update_partial_hours_requires_start_end_time(app_client):
    """LeaveUpdate 改 leave_hours<8 也要 start_time/end_time，缺則 422。"""
    client, session_factory = app_client
    emp_id = _setup_admin_and_employee(session_factory)
    leave_id = _setup_leave(session_factory, emp_id)
    _login(client)

    resp = client.put(
        f"/api/leaves/{leave_id}",
        json={
            "leave_hours": 4,
            # 缺 start_time / end_time
        },
    )
    assert (
        resp.status_code == 422
    ), f"PUT leave_hours<8 缺 start_time 應 422，實際 status={resp.status_code}, body={resp.json()}"
