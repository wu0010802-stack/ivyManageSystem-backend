"""Guardian create/update/delete 必須做班級 scope 守衛（稽核 2026-06-03 P1#8）。

read/write 不對稱：list_guardians 已呼叫 assert_student_access（F-025），但
create_guardian / update_guardian / delete_guardian 只用 require_staff_permission
守衛、以 bare Guardian.id 查詢，完全不做班級 scope → class-scoped 非 unrestricted
角色（principal / 自訂 role）可跨班讀寫他班家長 PII（IDOR + read-via-write）。

修法：三個 write 端點補上 assert_student_access（與 list_guardians 同簽名）。
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
from api.students import router as students_router
from models.database import Base, Classroom, Student, User
from models.guardian import Guardian
from utils.auth import hash_password


@pytest.fixture
def students_client(tmp_path):
    db_path = tmp_path / "guardian_crud_class_scope.sqlite"
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
    app.include_router(students_router)

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


def _seed_student_with_guardian(session):
    cls = Classroom(name="A班", is_active=True)
    session.add(cls)
    session.flush()
    stu = Student(
        student_id="S100",
        name="他班學生",
        classroom_id=cls.id,
        is_active=True,
        lifecycle_status="active",
    )
    session.add(stu)
    session.flush()
    g = Guardian(
        student_id=stu.id,
        name="他班家長",
        phone="0911-222-333",
        email="other@example.com",
        relation="母親",
        is_primary=True,
    )
    session.add(g)
    session.flush()
    return stu.id, g.id


GUARDIAN_PERMS = ["GUARDIANS_WRITE", "GUARDIANS_READ"]


class TestGuardianWriteClassScope:
    """class-scoped 非 unrestricted 角色（principal，無 employee_id → 無可存取班級）
    對他班 guardian 的寫入應被 assert_student_access 擋成 403。"""

    def test_principal_cannot_create_guardian_cross_class(self, students_client):
        client, sf = students_client
        with sf() as s:
            _create_user(s, "principal_u", "principal", GUARDIAN_PERMS)
            sid, _gid = _seed_student_with_guardian(s)
            s.commit()
        assert _login(client, "principal_u").status_code == 200
        res = client.post(
            f"/api/students/{sid}/guardians",
            json={"name": "新增越權家長"},
        )
        assert res.status_code == 403, res.text

    def test_principal_cannot_update_guardian_cross_class(self, students_client):
        client, sf = students_client
        with sf() as s:
            _create_user(s, "principal_u", "principal", GUARDIAN_PERMS)
            _sid, gid = _seed_student_with_guardian(s)
            s.commit()
        assert _login(client, "principal_u").status_code == 200
        # 空 body no-op PATCH 即可 read-via-write 洩漏 PII，必須先被 scope 擋下
        res = client.patch(f"/api/students/guardians/{gid}", json={})
        assert res.status_code == 403, res.text

    def test_principal_cannot_delete_guardian_cross_class(self, students_client):
        client, sf = students_client
        with sf() as s:
            _create_user(s, "principal_u", "principal", GUARDIAN_PERMS)
            _sid, gid = _seed_student_with_guardian(s)
            s.commit()
        assert _login(client, "principal_u").status_code == 200
        res = client.delete(f"/api/students/guardians/{gid}")
        assert res.status_code == 403, res.text


class TestGuardianWriteAdminStillWorks:
    """admin（unrestricted）正常路徑不可被守衛誤擋。"""

    def test_admin_can_create_update_delete(self, students_client):
        client, sf = students_client
        with sf() as s:
            _create_user(s, "admin_u", "admin", GUARDIAN_PERMS)
            sid, gid = _seed_student_with_guardian(s)
            s.commit()
        assert _login(client, "admin_u").status_code == 200

        created = client.post(
            f"/api/students/{sid}/guardians", json={"name": "正常新增家長"}
        )
        assert created.status_code == 201, created.text

        updated = client.patch(
            f"/api/students/guardians/{gid}", json={"phone": "0900-000-000"}
        )
        assert updated.status_code == 200, updated.text

        deleted = client.delete(f"/api/students/guardians/{gid}")
        assert deleted.status_code == 200, deleted.text
