"""PUT /api/portal/me/punch-pin 端點測試（教師自設打卡 PIN）。

TDD 六案例：
1. 正確 PIN 4 碼 → 200，hash 不落明文，punch_pin_set_at 有值
2. 非數字 PIN → 422
3. 太短 PIN（3 碼）→ 422
4. 家長 token → 403（require_non_parent_role 守衛）
5. PIN 6 碼（合法上界）→ 200，寫入正確
6. PIN 7 碼（超出上界）→ 422
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
from utils.auth import create_access_token, hash_password, verify_password

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
    """已登入教師的 TestClient，掛完整 portal router（含 require_non_parent_role 守衛）。

    改用完整 portal router（api.portal.router），使 require_non_parent_role()
    守衛真實生效，而非繞過它只掛 punch_pin_router。
    路由前綴由 portal_router 自帶（prefix="/api/portal"），端點路徑不變。
    """
    from api.portal import router as portal_router

    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(portal_router)

    with TestClient(app) as client:
        res = client.post(
            "/api/auth/login",
            json={"username": "pin_teacher", "password": "TempPass123"},
        )
        assert res.status_code == 200, f"登入失敗：{res.text}"
        yield client

    _ip_attempts.clear()
    _account_failures.clear()


@pytest.fixture
def portal_parent_user(test_db_session):
    """建立測試用家長 User（role='parent'，無 Employee），回傳 User 實例。

    家長不走 /api/auth/login（走 LIFF 綁定），因此直接建 DB 記錄並透過
    create_access_token 產生 JWT，模擬家長持有合法 token 後試圖呼叫
    員工端 API 的情境。
    """
    user = User(
        username="pin_parent",
        password_hash="!LINE_ONLY",
        role="parent",
        employee_id=None,
        permission_names=[],
        is_active=True,
        token_version=0,
    )
    test_db_session.add(user)
    test_db_session.commit()
    return user


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


def test_parent_role_cannot_set_pin(portal_client, portal_parent_user):
    """家長 token 應被 require_non_parent_role 守衛擋下，回 403。

    家長不走一般 login，直接用 create_access_token 建 JWT（模擬合法家長 token），
    以 per-request cookie 覆蓋 portal_client 的教師 session cookie，
    確認 portal router 層的守衛真實攔截。
    """
    parent_token = create_access_token(
        {
            "user_id": portal_parent_user.id,
            "employee_id": None,
            "role": "parent",
            "name": portal_parent_user.username,
            "permission_names": [],
            "token_version": portal_parent_user.token_version or 0,
        }
    )
    res = portal_client.put(
        "/api/portal/me/punch-pin",
        json={"pin": "1234"},
        cookies={"access_token": parent_token},
    )
    assert res.status_code == 403


def test_set_punch_pin_accepts_six_digits(
    portal_client, portal_employee, test_db_session
):
    """6 碼 PIN（合法上界）應回 200 並正確寫入。"""
    res = portal_client.put("/api/portal/me/punch-pin", json={"pin": "123456"})
    assert res.status_code == 200
    test_db_session.refresh(portal_employee)
    assert verify_password("123456", portal_employee.punch_pin_hash)


def test_set_punch_pin_rejects_seven_digits(portal_client):
    """7 碼 PIN（超出上界）應回 422。"""
    res = portal_client.put("/api/portal/me/punch-pin", json={"pin": "1234567"})
    assert res.status_code == 422
