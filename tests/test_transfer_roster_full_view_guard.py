"""銀行轉帳名冊匯出須 full-view 守衛（稽核 2026-06-03 P1#9）。

GET /api/salaries/{year}/{month}/transfer-roster 只用 require_staff_permission(SALARY_READ)
守衛，未做 full-view 限制 → principal / accountant 等持 SALARY_READ 但非 admin/hr 的
角色可越權匯出全員銀行帳號 + 淨薪 xlsx。對照 records.py export_all_salaries / festival.py
皆有 FULL_SALARY_ROLES / self-or-full 守衛。

修法：端點加 enforce_full_salary_view（services/finance/salary_access），非 admin/hr → 403。
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
from api.salary.transfer_roster import router as transfer_roster_router
from models.database import Base, User
from utils.auth import hash_password


@pytest.fixture
def roster_client(tmp_path):
    db_path = tmp_path / "transfer_roster_guard.sqlite"
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

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(transfer_roster_router, prefix="/api")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username, role, perms):
    u = User(
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permission_names=perms,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client, username):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "Passw0rd!"},
    )


ROSTER_URL = "/api/salaries/2026/5/transfer-roster?type=base"


def test_principal_with_salary_read_cannot_export_roster(roster_client):
    """principal 持 SALARY_READ 但非 admin/hr → 不可匯出全員銀行帳號名冊。"""
    client, sf = roster_client
    with sf() as s:
        _create_user(s, "principal_u", "principal", ["SALARY_READ"])
        s.commit()
    assert _login(client, "principal_u").status_code == 200
    res = client.get(ROSTER_URL)
    assert res.status_code == 403, res.text


def test_admin_can_export_roster(roster_client):
    """admin（FULL_SALARY_ROLES）正常路徑不可被守衛誤擋。"""
    client, sf = roster_client
    with sf() as s:
        _create_user(s, "admin_u", "admin", ["SALARY_READ"])
        s.commit()
    assert _login(client, "admin_u").status_code == 200
    res = client.get(ROSTER_URL)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
