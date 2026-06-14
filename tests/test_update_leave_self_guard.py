"""update_leave 須有自我編輯守衛（C3，bug hunt money-auth 2026-06-14）。

approve_leave / delete_leave / batch_approve / PUT quota 都有 is_self_approval 自我守衛，
唯獨 PUT /leaves/{id}（update_leave）整段沒有 → 持 LEAVES_WRITE 的 supervisor/hr/principal
（JWT 含 employee_id）可編輯本人已核准的扣薪假單，was_approved 分支退審觸發 sync.revert
刪除 Attendance → 薪資重算撤銷扣款＝替自己加薪，繞過 approve/delete 的自我守衛。

修法：update_leave 在 fetch 後比照 delete_leave 套用 is_self_approval → 403。
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
    db_path = tmp_path / "update_leave_self_guard.sqlite"
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


def _seed_leave(session, employee_id, status="approved"):
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


def test_supervisor_cannot_update_own_leave(leaves_client):
    """持 LEAVES_WRITE 的 supervisor 不可編輯本人假單（自我編輯守衛）。"""
    client, sf = leaves_client
    with sf() as s:
        _create_user(s, "sup", "supervisor", employee_id=100)
        leave_id = _seed_leave(s, employee_id=100, status="approved")
        s.commit()

    assert _login(client, "sup").status_code == 200
    res = client.put(f"/api/leaves/{leave_id}", json={"reason": "自我撤銷扣款"})
    assert res.status_code == 403, res.text

    # 假單狀態仍為 approved（未被退審→未觸發薪資撤扣）
    with sf() as s:
        lv = s.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        assert lv is not None
        assert lv.status == "approved"


def test_admin_updating_others_leave_not_blocked_by_self_guard(leaves_client):
    """admin（無 employee_id）編輯他人假單不應被自我編輯守衛誤擋（非 403）。"""
    client, sf = leaves_client
    with sf() as s:
        _create_user(s, "adm", "admin", employee_id=None)
        leave_id = _seed_leave(s, employee_id=200, status="pending")
        s.commit()

    assert _login(client, "adm").status_code == 200
    res = client.put(f"/api/leaves/{leave_id}", json={"reason": "管理員修訂"})
    # 自我守衛不得對「他人」假單觸發；其他業務驗證結果不限，但必不可為 403
    assert res.status_code != 403, res.text
