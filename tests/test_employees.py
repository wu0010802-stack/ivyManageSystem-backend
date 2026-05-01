"""tests/test_employees.py — 員工 CRUD 結構化錯誤回歸測試。

涵蓋：
- POST /api/employees 工號重複 → 400 with structured detail.code = EMPLOYEE_ID_DUPLICATE
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
from models.database import User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def employees_client(tmp_path):
    db_path = tmp_path / "employees.sqlite"
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


def _login_admin(client, session_factory):
    """建立 admin 帳號（無 employee_id）並登入，回傳 admin user id。"""
    with session_factory() as s:
        u = User(
            username="admin",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permissions=Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE,
            employee_id=None,
            is_active=True,
            must_change_password=False,
        )
        s.add(u)
        s.commit()
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "Temp123456"}
    )
    assert resp.status_code == 200, resp.json()


def test_create_employee_duplicate_id_returns_structured_detail(employees_client):
    """POST 同 employee_id 兩次 → 第二次 400 with structured detail."""
    client, sf = employees_client
    _login_admin(client, sf)

    payload = {
        "employee_id": "DUP001",
        "name": "甲",
        "employee_type": "regular",
    }
    resp1 = client.post("/api/employees", json=payload)
    assert resp1.status_code == 201, resp1.json()

    payload2 = {
        "employee_id": "DUP001",
        "name": "乙",
        "employee_type": "regular",
    }
    resp2 = client.post("/api/employees", json=payload2)
    assert resp2.status_code == 400, resp2.json()
    detail = resp2.json()["detail"]
    assert detail["code"] == "EMPLOYEE_ID_DUPLICATE"
    assert detail["context"]["employee_id"] == "DUP001"
    assert "已存在" in detail["message"]
