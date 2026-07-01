"""tests/test_employee_reset_punch_pin.py — 管理端重置員工打卡 PIN 端點測試。

TDD 兩案例：
1. 有 ATTENDANCE_WRITE 的管理員重置 → 200，punch_pin_hash / punch_pin_set_at 清空
2. 無 ATTENDANCE_WRITE 的帳號重置 → 403

端點：POST /api/employees/{employee_id}/reset-punch-pin
"""

import os
import sys
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.employees import router as employees_router
from models.database import Employee, User
from utils.auth import hash_password
from utils.permissions import Permission

# ──────────────────────────── Helpers ────────────────────────────


def _login(client, username, password):
    """呼叫登入端點，TestClient 自動保留 cookie，後續請求即帶驗證。"""
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )


# ──────────────────────────── Fixtures ────────────────────────────


@pytest.fixture
def admin_client(test_db_session):
    """有 ATTENDANCE_WRITE 的管理員 TestClient（已登入）。

    使用 conftest.py 的 test_db_session（已 swap 全域 engine），
    確保 TestClient 裡的端點 get_session() 走同一個 SQLite 測試 DB。
    """
    admin_user = User(
        username="pin_reset_admin",
        password_hash=hash_password("Admin123456"),
        role="admin",
        permission_names=[Permission.ATTENDANCE_WRITE.value],
        is_active=True,
        must_change_password=False,
    )
    test_db_session.add(admin_user)
    test_db_session.commit()

    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(employees_router)

    with TestClient(app) as client:
        res = _login(client, "pin_reset_admin", "Admin123456")
        assert res.status_code == 200, f"admin 登入失敗：{res.text}"
        yield client

    _ip_attempts.clear()
    _account_failures.clear()


@pytest.fixture
def readonly_client(test_db_session):
    """無任何權限的帳號 TestClient（已登入）。

    使用 conftest.py 的 test_db_session（已 swap 全域 engine），
    確保 TestClient 裡的端點 get_session() 走同一個 SQLite 測試 DB。
    """
    readonly_user = User(
        username="pin_reset_readonly",
        password_hash=hash_password("Read123456"),
        role="admin",
        permission_names=[],  # 無任何權限
        is_active=True,
        must_change_password=False,
    )
    test_db_session.add(readonly_user)
    test_db_session.commit()

    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(employees_router)

    with TestClient(app) as client:
        res = _login(client, "pin_reset_readonly", "Read123456")
        assert res.status_code == 200, f"readonly 登入失敗：{res.text}"
        yield client

    _ip_attempts.clear()
    _account_failures.clear()


# ──────────────────────────── Tests ────────────────────────────


def test_reset_clears_pin(admin_client, test_db_session):
    """有 ATTENDANCE_WRITE 的管理員重置 PIN → 200，punch_pin_hash / punch_pin_set_at 清空。"""
    emp = Employee(
        employee_id="E950",
        name="陳老師",
        is_active=True,
        punch_pin_hash=hash_password("1234"),
        punch_pin_set_at=datetime(2025, 1, 1),  # 預先設定時間，確保 reset 真正清空
    )
    test_db_session.add(emp)
    test_db_session.commit()

    res = admin_client.post(f"/api/employees/{emp.id}/reset-punch-pin")
    assert res.status_code == 200
    assert res.json()["message"] == "打卡 PIN 已重置"

    test_db_session.refresh(emp)
    assert emp.punch_pin_hash is None
    assert emp.punch_pin_set_at is None


def test_reset_requires_permission(readonly_client, test_db_session):
    """無 ATTENDANCE_WRITE 的帳號重置 → 403。"""
    emp = Employee(
        employee_id="E951",
        name="林老師",
        is_active=True,
    )
    test_db_session.add(emp)
    test_db_session.commit()

    res = readonly_client.post(f"/api/employees/{emp.id}/reset-punch-pin")
    assert res.status_code == 403
