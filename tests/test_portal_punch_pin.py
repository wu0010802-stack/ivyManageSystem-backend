"""PUT /api/portal/me/punch-pin 端點測試（教師自設打卡 PIN）。

TDD 三案例：
1. 正確 PIN 4 碼 → 200，hash 不落明文，punch_pin_set_at 有值
2. 非數字 PIN → 422
3. 太短 PIN（3 碼）→ 422
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import Employee, User
from utils.auth import hash_password, verify_password

# ──────────────────────────── Fixtures ────────────────────────────


@pytest.fixture
def portal_employee(test_db_session):
    """建立測試用教師 Employee + User（role='teacher'），回傳 Employee 實例。

    使用 conftest.py 的 test_db_session（已 swap 全域 engine），
    確保 TestClient 裡的端點 get_session() 走同一個 SQLite 測試 DB。
    """
    emp = Employee(
        employee_id="T_PIN_01",
        name="PIN測試教師",
        base_salary=32000,
        is_active=True,
    )
    test_db_session.add(emp)
    test_db_session.flush()

    user = User(
        username="pin_teacher",
        password_hash=hash_password("TempPass123"),
        role="teacher",
        employee_id=emp.id,
        permission_names=[],
        is_active=True,
    )
    test_db_session.add(user)
    test_db_session.commit()
    return emp


@pytest.fixture
def portal_client(portal_employee):
    """已登入教師的 TestClient（cookie session 維持登入狀態）。

    portal_employee 為 dependent fixture，確保 Employee/User 在 TestClient
    建立前已寫入 DB；登入後 cookie 自動附帶於後續請求。
    """
    from api.portal.punch_pin import router as punch_pin_router

    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(punch_pin_router, prefix="/api/portal")

    with TestClient(app) as client:
        res = client.post(
            "/api/auth/login",
            json={"username": "pin_teacher", "password": "TempPass123"},
        )
        assert res.status_code == 200, f"登入失敗：{res.text}"
        yield client

    _ip_attempts.clear()
    _account_failures.clear()


# ──────────────────────────── Tests ────────────────────────────


def test_set_punch_pin_hashes_and_persists(
    portal_client, portal_employee, test_db_session
):
    """正確 PIN 應回 200，hash 不等於明文，且 punch_pin_set_at 有值。"""
    res = portal_client.put("/api/portal/me/punch-pin", json={"pin": "1234"})
    assert res.status_code == 200
    test_db_session.refresh(portal_employee)
    assert portal_employee.punch_pin_hash
    assert portal_employee.punch_pin_hash != "1234"  # 不落明文
    assert verify_password("1234", portal_employee.punch_pin_hash)
    assert portal_employee.punch_pin_set_at is not None


def test_set_punch_pin_rejects_non_digit(portal_client):
    """含非數字字元的 PIN 應回 422。"""
    res = portal_client.put("/api/portal/me/punch-pin", json={"pin": "12ab"})
    assert res.status_code == 422


def test_set_punch_pin_rejects_too_short(portal_client):
    """3 碼 PIN（< 4 位）應回 422。"""
    res = portal_client.put("/api/portal/me/punch-pin", json={"pin": "123"})
    assert res.status_code == 422
