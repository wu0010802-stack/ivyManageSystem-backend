"""PUT /api/auth/users/{id} 提權稽核 in-session 化驗證。

Why: 角色/權限變更是提權敏感操作，原本只設 request.state.audit_summary 交
AuditMiddleware fire-and-forget 背景寫入——threadpool 故障/DB 連線中斷時主資料
已 commit 但稽核丟失。對齊金流端點（api/salary/manual_adjust.py）的
write_audit_in_session pattern：稽核與主交易共生死。

兩個關鍵性質：
1. 不掛 middleware 也必須有稽核 row（證明寫入發生在 endpoint 自身 session 內）
2. 掛上 middleware 恰好 1 筆（audit_skip 旗標生效，不重複寫兩筆）
"""

from __future__ import annotations

import json
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
from models.database import AuditLog, Base, User
from utils.audit import AuditMiddleware
from utils.auth import hash_password


def _make_client(tmp_path, *, with_middleware: bool):
    db_path = tmp_path / "user-audit.sqlite"
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
    if with_middleware:
        app.add_middleware(AuditMiddleware)
    app.include_router(auth_router)

    def cleanup():
        _ip_attempts.clear()
        _account_failures.clear()
        base_module._engine = old_engine
        base_module._SessionFactory = old_session_factory
        engine.dispose()

    return app, session_factory, cleanup


@pytest.fixture
def bare_client(tmp_path):
    """無 AuditMiddleware：稽核必須由 endpoint 自身 in-session 寫入。"""
    app, sf, cleanup = _make_client(tmp_path, with_middleware=False)
    with TestClient(app) as client:
        yield client, sf
    cleanup()


@pytest.fixture
def middleware_client(tmp_path):
    """掛 AuditMiddleware：驗證 audit_skip 生效、不重複寫兩筆。"""
    app, sf, cleanup = _make_client(tmp_path, with_middleware=True)
    with TestClient(app) as client:
        yield client, sf
    cleanup()


def _seed_users(sf):
    with sf() as s:
        s.add(
            User(
                username="root_admin",
                password_hash=hash_password("AdminPass1234"),
                role="admin",
                permission_names=["*"],
                is_active=True,
                must_change_password=False,
            )
        )
        target = User(
            username="target_staff",
            password_hash=hash_password("StaffPass1234"),
            role="staff",
            permission_names=["EMPLOYEES_READ"],
            is_active=True,
            must_change_password=False,
        )
        s.add(target)
        s.flush()
        target_id = target.id
        s.commit()
    return target_id


def _login_admin(client):
    res = client.post(
        "/api/auth/login",
        json={"username": "root_admin", "password": "AdminPass1234"},
    )
    assert res.status_code == 200, res.text


def _wait_for_background_audits():
    """AuditMiddleware fire-and-forget；等背景 task 完成避免假陰/陽性。"""
    import asyncio

    from utils.audit import _background_tasks

    async def drain():
        if _background_tasks:
            await asyncio.gather(*list(_background_tasks), return_exceptions=True)

    try:
        loop = asyncio.get_event_loop()
        if not loop.is_running():
            loop.run_until_complete(drain())
    except RuntimeError:
        pass


def _user_audit_rows(sf):
    with sf() as s:
        return (
            s.query(AuditLog)
            .filter(AuditLog.entity_type == "user", AuditLog.action == "UPDATE")
            .all()
        )


def test_role_change_audit_written_in_session_without_middleware(bare_client):
    """不掛 middleware：改 role 後 audit_logs 必有紀錄（與主交易同生死）。"""
    client, sf = bare_client
    target_id = _seed_users(sf)
    _login_admin(client)

    res = client.put(
        f"/api/auth/users/{target_id}",
        json={"role": "hr", "permission_names": ["EMPLOYEES_READ", "LEAVES_READ"]},
    )
    assert res.status_code == 200, res.text

    rows = _user_audit_rows(sf)
    assert len(rows) == 1, (
        f"role/permission 變更應在同交易內寫入 1 筆稽核，實得 {len(rows)} 筆"
        "（fire-and-forget middleware 不可靠，提權稽核必須 in-session）"
    )
    log = rows[0]
    assert log.entity_id == str(target_id)
    assert "角色 staff → hr" in log.summary
    assert log.changes is not None
    changes = json.loads(log.changes)
    assert changes["old_role"] == "staff"
    assert changes["new_role"] == "hr"
    assert "LEAVES_READ" in changes["permissions_added"]


def test_role_change_audit_not_duplicated_with_middleware(middleware_client):
    """掛 middleware：恰好 1 筆（in-session 寫入後 audit_skip 阻止二次寫入）。"""
    client, sf = middleware_client
    target_id = _seed_users(sf)
    _login_admin(client)

    res = client.put(
        f"/api/auth/users/{target_id}",
        json={"role": "hr", "permission_names": ["EMPLOYEES_READ"]},
    )
    assert res.status_code == 200, res.text

    _wait_for_background_audits()

    rows = _user_audit_rows(sf)
    assert (
        len(rows) == 1
    ), f"應恰好 1 筆 user audit，實得 {len(rows)}（缺寫或 middleware 重複寫）"
    assert "角色 staff → hr" in rows[0].summary


def test_pure_deactivation_still_audited_as_soft_delete(middleware_client):
    """純停用（軟刪語意）維持既有 mark_soft_delete → middleware 稽核路徑。"""
    client, sf = middleware_client
    target_id = _seed_users(sf)
    _login_admin(client)

    res = client.put(f"/api/auth/users/{target_id}", json={"is_active": False})
    assert res.status_code == 200, res.text

    _wait_for_background_audits()

    rows = _user_audit_rows(sf)
    assert len(rows) == 1, f"純停用應恰好 1 筆 user audit，實得 {len(rows)}"
    assert "軟刪" in rows[0].summary
