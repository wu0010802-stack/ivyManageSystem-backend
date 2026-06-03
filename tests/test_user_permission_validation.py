"""RA-HIGH-1b：建立/更新 user 時，permission_names 的 code/scope 須驗證。

漏洞：POST/PUT /api/auth/users 把 permission_names 原樣寫入，未驗證每筆是否為
合法 code、scope 後綴是否只掛在 scope-aware code 上。配合 RA-HIGH-1a 的
fail-closed，誤帶 `SALARY_READ:own_class` 雖不再被當全域放行，但仍應在寫入時
就拒絕（防殘留無效資料 + 給操作者即時回饋）。

守護：validate_permission_names 純函式 + create/update user 路徑呼叫，非法項回 422。

依 tests/test_user_management_authz.py 的 cookie-based auth_client 模式實作。
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
from models.database import Base, User

# 註冊 roles 表（get_role_default_permissions 對 role=None 路徑會查 roles）
from models.permission_models import Role
from utils.auth import hash_password
from utils.permissions import ROLE_TEMPLATES

_STRONG_PW = "Strong!@#1234"


@pytest.fixture
def auth_client(tmp_path):
    """隔離 sqlite 測試 app；admin 以 cookie 登入（同 test_user_management_authz）。"""
    db_path = tmp_path / "perm-validation.sqlite"
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
    # seed core roles 供 role=None → 角色預設權限路徑查詢
    with session_factory() as s:
        for code, perms in ROLE_TEMPLATES.items():
            s.add(Role(code=code, label=code, permissions=list(perms), is_core=True))
        s.commit()
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def admin_login(auth_client):
    """建立 admin（wildcard）並登入；回傳已帶 cookie 的 client。"""
    client, session_factory = auth_client
    with session_factory() as session:
        session.add(
            User(
                username="root_admin",
                password_hash=hash_password("AdminPass1234"),
                role="admin",
                permission_names=["*"],
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    r = client.post(
        "/api/auth/login", json={"username": "root_admin", "password": "AdminPass1234"}
    )
    assert r.status_code == 200, r.text
    return client


def _create_payload(username, **overrides):
    payload = {"username": username, "password": _STRONG_PW, "role": "teacher"}
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# POST /api/auth/users
# ---------------------------------------------------------------------------


def test_reject_scope_on_non_scope_aware_code(admin_login):
    """非 scope-aware code 帶 scope 後綴 → 422。"""
    r = admin_login.post(
        "/api/auth/users",
        json=_create_payload("u1", permission_names=["SALARY_READ:own_class"]),
    )
    assert r.status_code == 422, r.text


def test_accept_scope_on_scope_aware_code(admin_login):
    """scope-aware code 帶合法 scope 後綴 → 建立成功（201）。"""
    r = admin_login.post(
        "/api/auth/users",
        json=_create_payload("u2", permission_names=["STUDENTS_READ:own_class"]),
    )
    assert r.status_code in (200, 201), r.text


def test_reject_unknown_code(admin_login):
    """非法 code → 422。"""
    r = admin_login.post(
        "/api/auth/users",
        json=_create_payload("u3", permission_names=["NOT_A_CODE"]),
    )
    assert r.status_code == 422, r.text


def test_reject_bad_scope_value(admin_login):
    """scope-aware code 帶非法 scope 值 → 422。"""
    r = admin_login.post(
        "/api/auth/users",
        json=_create_payload("u4", permission_names=["STUDENTS_READ:bogus"]),
    )
    assert r.status_code == 422, r.text


def test_accept_bare_code(admin_login):
    """合法 bare code（無 scope）→ 201。"""
    r = admin_login.post(
        "/api/auth/users",
        json=_create_payload("u5", permission_names=["SALARY_READ"]),
    )
    assert r.status_code in (200, 201), r.text


def test_accept_wildcard(admin_login):
    """wildcard '*' 視為合法（admin 授全權）→ 201。"""
    r = admin_login.post(
        "/api/auth/users",
        json=_create_payload("u6", role="admin", permission_names=["*"]),
    )
    assert r.status_code in (200, 201), r.text


def test_create_none_permission_names_ok(admin_login):
    """permission_names 未提供（None）→ 套角色預設，不觸發驗證 → 201。"""
    r = admin_login.post(
        "/api/auth/users",
        json={"username": "u7", "password": _STRONG_PW, "role": "teacher"},
    )
    assert r.status_code in (200, 201), r.text


# ---------------------------------------------------------------------------
# PUT /api/auth/users/{id}
# ---------------------------------------------------------------------------


def test_update_reject_scope_on_non_scope_aware_code(admin_login, auth_client):
    """更新時非 scope-aware code 帶 scope → 422。"""
    _, session_factory = auth_client
    with session_factory() as session:
        target = User(
            username="upd_target",
            password_hash=hash_password(_STRONG_PW),
            role="teacher",
            permission_names=["EMPLOYEES_READ"],
            is_active=True,
            must_change_password=False,
        )
        session.add(target)
        session.commit()
        target_id = target.id

    r = admin_login.put(
        f"/api/auth/users/{target_id}",
        json={"permission_names": ["SALARY_READ:own_class"]},
    )
    assert r.status_code == 422, r.text


def test_update_accept_scope_aware_code(admin_login, auth_client):
    """更新時 scope-aware code 帶合法 scope → 200。"""
    _, session_factory = auth_client
    with session_factory() as session:
        target = User(
            username="upd_target2",
            password_hash=hash_password(_STRONG_PW),
            role="teacher",
            permission_names=["EMPLOYEES_READ"],
            is_active=True,
            must_change_password=False,
        )
        session.add(target)
        session.commit()
        target_id = target.id

    r = admin_login.put(
        f"/api/auth/users/{target_id}",
        json={"permission_names": ["STUDENTS_READ:own_class"]},
    )
    assert r.status_code == 200, r.text
