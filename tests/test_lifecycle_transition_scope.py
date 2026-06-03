"""transition_student_lifecycle 須做班級 scope 守衛（稽核 2026-06-03 P2-a）。

POST /students/{id}/lifecycle 只用 require_staff_permission(STUDENTS_LIFECYCLE_WRITE)
守衛、bare Student.id 查詢，未做班級 scope → 持 STUDENTS_LIFECYCLE_WRITE:own_class 的
class-scoped 角色可對他班學生觸發退學/休學/轉出/畢業（破壞性狀態變更 + 取消接送通知/
軟刪才藝報名）。STUDENTS_LIFECYCLE_WRITE 是 scope-aware perm（permscope01 seeds
own_class/all），故比照 get_students 用 assert_student_access(code=...) scope-aware 守衛。
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
from utils.auth import hash_password


@pytest.fixture
def students_client(tmp_path):
    db_path = tmp_path / "lifecycle_transition_scope.sqlite"
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
        "/api/auth/login", json={"username": username, "password": "Passw0rd!"}
    )


def _seed_active_student(session):
    cls = Classroom(name="A班", is_active=True)
    session.add(cls)
    session.flush()
    stu = Student(
        student_id="S300",
        name="他班學生",
        classroom_id=cls.id,
        is_active=True,
        lifecycle_status="active",
    )
    session.add(stu)
    session.flush()
    return stu.id


def test_own_class_scoped_role_cannot_transition_other_class(students_client):
    """持 STUDENTS_LIFECYCLE_WRITE:own_class 但無可存取班級者不可轉移他班學生。"""
    client, sf = students_client
    with sf() as s:
        _create_user(
            s, "principal_u", "principal", ["STUDENTS_LIFECYCLE_WRITE:own_class"]
        )
        sid = _seed_active_student(s)
        s.commit()
    assert _login(client, "principal_u").status_code == 200
    res = client.post(f"/api/students/{sid}/lifecycle", json={"to_status": "on_leave"})
    assert res.status_code == 403, res.text

    with sf() as s:
        # 狀態未被改動
        st = s.query(Student).filter(Student.id == sid).first()
        assert st.lifecycle_status == "active"


def test_admin_can_transition(students_client):
    """admin（unrestricted）正常轉移不被守衛誤擋。"""
    client, sf = students_client
    with sf() as s:
        _create_user(s, "admin_u", "admin", ["STUDENTS_LIFECYCLE_WRITE"])
        sid = _seed_active_student(s)
        s.commit()
    assert _login(client, "admin_u").status_code == 200
    res = client.post(f"/api/students/{sid}/lifecycle", json={"to_status": "on_leave"})
    assert res.status_code == 200, res.text

    with sf() as s:
        st = s.query(Student).filter(Student.id == sid).first()
        assert st.lifecycle_status == "on_leave"
