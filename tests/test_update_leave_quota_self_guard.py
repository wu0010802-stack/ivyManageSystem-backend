"""update_leave_quota 須有自我守衛（稽核 2026-06-03 P2-b，職責分離）。

PUT /leaves/quotas/{id} 接受 free-form total_hours，僅擋負數，無上限、無 self-guard →
持 LEAVES_WRITE 者可把本人配額灌成超大值；配額直接決定假單是否扣薪（_guard_leave_quota
以 remaining 判定），灌爆後本人原本超額/扣薪的假別會被視為仍有餘額轉為不扣薪。

修法：比照 leaves/overtimes 的 is_self_approval idiom，本人配額 403。
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
from api.leaves import router as leaves_router
from models.database import Base, User
from models.leave import LeaveQuota
from utils.auth import hash_password


@pytest.fixture
def quota_client(tmp_path):
    db_path = tmp_path / "update_leave_quota_self_guard.sqlite"
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
    app.include_router(leaves_router)  # 含 quota_router，prefix=/api

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username, role, *, employee_id=None):
    u = User(
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permission_names=["LEAVES_WRITE"],
        is_active=True,
        must_change_password=False,
        employee_id=employee_id,
    )
    session.add(u)
    session.flush()
    return u


def _login(client, username):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Passw0rd!"}
    )


def _seed_quota(session, employee_id):
    q = LeaveQuota(
        employee_id=employee_id, year=2026, leave_type="annual", total_hours=80.0
    )
    session.add(q)
    session.flush()
    return q.id


def test_supervisor_cannot_inflate_own_quota(quota_client):
    """持 LEAVES_WRITE 的 supervisor 不可調整本人配額。"""
    client, sf = quota_client
    with sf() as s:
        _create_user(s, "sup", "supervisor", employee_id=100)
        qid = _seed_quota(s, employee_id=100)
        s.commit()
    assert _login(client, "sup").status_code == 200
    res = client.put(f"/api/leaves/quotas/{qid}", json={"total_hours": 9999.0})
    assert res.status_code == 403, res.text

    with sf() as s:
        q = s.query(LeaveQuota).filter(LeaveQuota.id == qid).first()
        assert q.total_hours == 80.0  # 未被灌爆


def test_admin_can_update_others_quota(quota_client):
    """admin（無 employee_id）正常調整他人配額不被誤擋。"""
    client, sf = quota_client
    with sf() as s:
        _create_user(s, "adm", "admin", employee_id=None)
        qid = _seed_quota(s, employee_id=200)
        s.commit()
    assert _login(client, "adm").status_code == 200
    res = client.put(f"/api/leaves/quotas/{qid}", json={"total_hours": 120.0})
    assert res.status_code == 200, res.text

    with sf() as s:
        q = s.query(LeaveQuota).filter(LeaveQuota.id == qid).first()
        assert q.total_hours == 120.0
