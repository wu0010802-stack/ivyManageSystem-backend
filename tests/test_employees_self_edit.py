"""tests/test_employees_self_edit.py — 員工自我編輯矩陣測試。

涵蓋：
- 員工本人改自己 phone（非敏感）→ 200
- 員工本人改自己 base_salary（單一敏感）→ 403 SELF_FINANCE_EDIT_FORBIDDEN
- 員工本人改 base_salary + classroom_id + phone（多敏感+一般）→ 403, context.fields
  含 base_salary / classroom_id（不含 phone）
- 純管理員（無 employee_id）改別人薪資 → 不應因 self-edit 守衛被擋
- HR（有 employee_id 但非目標）改別人薪資 → 不應因 self-edit 守衛被擋

作為前端鎖頭白名單的後端 source of truth。
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.employees import router as employees_router
from models.base import Base
from models.database import Employee, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def selfedit_client(tmp_path):
    db_path = tmp_path / "selfedit.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(employees_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _make_employee(session, *, name, base_salary=30000, classroom_id=None):
    emp = Employee(
        employee_id=f"E_{name}",
        name=name,
        base_salary=base_salary,
        employee_type="regular",
        classroom_id=classroom_id,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_user(session, *, username, permissions, employee_id=None, role="admin"):
    u = User(
        username=username,
        password_hash=hash_password("Temp123456"),
        role=role,
        permissions=permissions,
        employee_id=employee_id,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client, username):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Temp123456"}
    )


# ── 案例 1：本人改非敏感欄位 → 200 ──────────────────────────────────


def test_self_edit_only_non_sensitive_fields_allowed(selfedit_client):
    """員工本人改 phone → 200，守衛不擋一般資料維護。"""
    client, sf = selfedit_client
    with sf() as s:
        emp = _make_employee(s, name="自編甲")
        _make_user(
            s,
            username="self",
            permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
            employee_id=emp.id,
        )
        s.commit()
        emp_id = emp.id

    assert _login(client, "self").status_code == 200
    res = client.put(f"/api/employees/{emp_id}", json={"phone": "0911000000"})
    assert res.status_code == 200, res.json()


# ── 案例 2：本人改單一敏感欄位 → 403 SELF_FINANCE_EDIT_FORBIDDEN ──


def test_self_edit_single_sensitive_field_blocked(selfedit_client):
    """員工本人改 base_salary → 403 with structured detail."""
    client, sf = selfedit_client
    with sf() as s:
        emp = _make_employee(s, name="自編乙")
        _make_user(
            s,
            username="self",
            permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
            employee_id=emp.id,
        )
        s.commit()
        emp_id = emp.id

    assert _login(client, "self").status_code == 200
    res = client.put(f"/api/employees/{emp_id}", json={"base_salary": 99999})
    assert res.status_code == 403
    detail = res.json()["detail"]
    assert detail["code"] == "SELF_FINANCE_EDIT_FORBIDDEN"
    assert "不得修改自己" in detail["message"]
    assert detail["context"]["fields"] == ["base_salary"]


# ── 案例 3：本人改多敏感+一般 → 403，context.fields 只列敏感 ──


def test_self_edit_multiple_sensitive_fields_listed(selfedit_client):
    """混合 base_salary + classroom_id + phone：phone 不算敏感，不應出現在 fields。"""
    client, sf = selfedit_client
    with sf() as s:
        emp = _make_employee(s, name="自編丙")
        _make_user(
            s,
            username="self",
            permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
            employee_id=emp.id,
        )
        s.commit()
        emp_id = emp.id

    assert _login(client, "self").status_code == 200
    res = client.put(
        f"/api/employees/{emp_id}",
        json={"base_salary": 50000, "classroom_id": 1, "phone": "0911"},
    )
    assert res.status_code == 403
    detail = res.json()["detail"]
    assert detail["code"] == "SELF_FINANCE_EDIT_FORBIDDEN"
    assert set(detail["context"]["fields"]) == {"base_salary", "classroom_id"}
    # phone 不算敏感，不出現
    assert "phone" not in detail["context"]["fields"]


# ── 案例 4：純管理員（無 employee_id）改別人 → 不被 self-edit 擋 ─


def test_pure_admin_editing_other_employee_not_self_blocked(selfedit_client):
    """admin 帳號 employee_id=None，改別人 base_salary 不該被 self-edit 擋。

    可能因其他驗證（如 minimum_wage）回 400，但不該回 SELF_FINANCE_EDIT_FORBIDDEN。
    """
    client, sf = selfedit_client
    with sf() as s:
        target = _make_employee(s, name="目標員工")
        _make_user(
            s,
            username="admin",
            permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
            employee_id=None,  # 純管理員
        )
        s.commit()
        target_id = target.id

    assert _login(client, "admin").status_code == 200
    res = client.put(f"/api/employees/{target_id}", json={"base_salary": 38000})
    assert res.status_code != 403 or (
        isinstance(res.json().get("detail"), dict)
        and res.json()["detail"].get("code") != "SELF_FINANCE_EDIT_FORBIDDEN"
    )


# ── 案例 5：HR（有 employee_id 但非目標）改別人 → 不被擋 ────────


def test_hr_editing_other_employee_not_self_blocked(selfedit_client):
    """HR 自己有 employee_id 但 target 是別人 → 不該被 self-edit 擋。"""
    client, sf = selfedit_client
    with sf() as s:
        hr_emp = _make_employee(s, name="HR人員")
        target = _make_employee(s, name="他人")
        _make_user(
            s,
            username="hr",
            permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
            employee_id=hr_emp.id,
        )
        s.commit()
        target_id = target.id

    assert _login(client, "hr").status_code == 200
    res = client.put(f"/api/employees/{target_id}", json={"base_salary": 38000})
    assert res.status_code != 403 or (
        isinstance(res.json().get("detail"), dict)
        and res.json()["detail"].get("code") != "SELF_FINANCE_EDIT_FORBIDDEN"
    )
