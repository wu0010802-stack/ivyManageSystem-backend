"""delete_leave 須有自我刪除守衛 + approver 角色資格檢查（稽核 2026-06-03 P1#6）。

approve_leave / batch_approve 都有 is_self_approval 自我守衛 + assert_approver_eligible，
但 delete_leave 兩者皆無 → 持 LEAVES_WRITE 的 supervisor/hr/principal（JWT 含 employee_id）
可 DELETE 本人已核准的扣薪假單，was_approved 分支觸發薪資重算撤銷扣款＝替自己加薪，
繞過 approve 的自我核准守衛；亦可刪他人假單而不受 approver 資格限制。

修法：delete_leave 在 fetch 後比照 approve_leave 套用 is_self_approval + assert_approver_eligible。
"""

import os
import sys
from datetime import date

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
from models.leave import LeaveRecord
from utils.auth import hash_password


@pytest.fixture
def leaves_client(tmp_path):
    db_path = tmp_path / "delete_leave_self_guard.sqlite"
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
    app.include_router(leaves_router)  # leaves router 自帶 prefix="/api"

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


def _seed_leave(session, employee_id, status="pending"):
    lv = LeaveRecord(
        employee_id=employee_id,
        leave_type="personal",
        start_date=date(2026, 5, 22),
        end_date=date(2026, 5, 22),
        leave_hours=8.0,
        status=status,
    )
    session.add(lv)
    session.flush()
    return lv.id


def test_supervisor_cannot_delete_own_leave(leaves_client):
    """持 LEAVES_WRITE 的 supervisor 不可刪除本人假單（自我刪除守衛）。"""
    client, sf = leaves_client
    with sf() as s:
        _create_user(s, "sup", "supervisor", employee_id=100)
        leave_id = _seed_leave(s, employee_id=100)
        s.commit()

    assert _login(client, "sup").status_code == 200
    res = client.delete(f"/api/leaves/{leave_id}")
    assert res.status_code == 403, res.text

    # 假單仍在（未被刪）
    with sf() as s:
        assert (
            s.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first() is not None
        )


def test_admin_can_delete_others_leave(leaves_client):
    """admin（無 employee_id）刪除他人 pending 假單正常路徑不被守衛誤擋。"""
    client, sf = leaves_client
    with sf() as s:
        _create_user(s, "adm", "admin", employee_id=None)
        leave_id = _seed_leave(s, employee_id=200)
        s.commit()

    assert _login(client, "adm").status_code == 200
    res = client.delete(f"/api/leaves/{leave_id}")
    assert res.status_code == 200, res.text

    with sf() as s:
        assert s.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first() is None
