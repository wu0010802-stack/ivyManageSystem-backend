"""家長綁定碼 / 裝置碼 / 撤銷裝置端點須班級 scope 守衛（稽核 2026-06-03 P1#7）。

create_binding_code / create_device_setup_code / revoke_guardian_devices 三個端點只用
require_staff_permission(GUARDIANS_WRITE) 守衛、以 bare Guardian.id 查詢，未做班級
scope → class-scoped 非 unrestricted 角色（principal / 自訂 role）可為他班任一孩童
簽發家長端綁定碼（明碼回傳）→ 以該碼於家長端綁定帳號取得跨班 account-linkage。

修法：三端點補 assert_student_access（以 guardian.student_id 判定）。
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
from api.guardians_admin import router as guardians_admin_router
from models.database import Base, Classroom, Student, User
from models.guardian import Guardian
from utils.auth import hash_password


@pytest.fixture
def bind_client(tmp_path):
    db_path = tmp_path / "guardian_binding_admin_scope.sqlite"
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
    app.include_router(guardians_admin_router, prefix="/api")

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


def _seed_guardian(session):
    cls = Classroom(name="A班", is_active=True)
    session.add(cls)
    session.flush()
    stu = Student(
        student_id="S200",
        name="他班學生",
        classroom_id=cls.id,
        is_active=True,
        lifecycle_status="active",
    )
    session.add(stu)
    session.flush()
    g = Guardian(student_id=stu.id, name="他班家長", relation="母親", is_primary=True)
    session.add(g)
    session.flush()
    return g.id


PERMS = ["GUARDIANS_WRITE", "GUARDIANS_READ"]


@pytest.mark.parametrize(
    "path_suffix",
    ["binding-code", "device-setup-code", "revoke-devices"],
)
def test_principal_cannot_issue_cross_class(bind_client, path_suffix):
    """class-scoped principal（無 employee_id → 無可存取班級）不可對他班 guardian 簽發/撤銷。"""
    client, sf = bind_client
    with sf() as s:
        _create_user(s, "principal_u", "principal", PERMS)
        gid = _seed_guardian(s)
        s.commit()
    assert _login(client, "principal_u").status_code == 200
    res = client.post(f"/api/guardians/{gid}/{path_suffix}")
    assert res.status_code == 403, res.text


def test_admin_can_issue_binding_code(bind_client):
    """admin（unrestricted）正常路徑不可被守衛誤擋。"""
    client, sf = bind_client
    with sf() as s:
        _create_user(s, "admin_u", "admin", PERMS)
        gid = _seed_guardian(s)
        s.commit()
    assert _login(client, "admin_u").status_code == 200
    res = client.post(f"/api/guardians/{gid}/binding-code")
    assert res.status_code == 200, res.text
    assert "code" in res.json()
