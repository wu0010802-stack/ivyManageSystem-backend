"""student-enrollment roster 須依班級 scope 過濾（稽核 2026-06-03 P2-c）。

GET /student-enrollment/roster 與 roster.pdf 只用 require_staff_permission(STUDENTS_READ)
守衛（且以 `_: None` 忽略 current_user），查 Classroom/Student 完全未做 scope 過濾 →
持 STUDENTS_READ:own_class 的 class-scoped 角色可讀全園各班學生姓名冊 + 教師名單。
對照 get_students 以 is_unrestricted/accessible_classroom_ids 過濾。

修法：roster 加 scope 過濾（class-scoped 只看自己班）；roster.pdf 委派時傳 current_user。
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
from api.student_enrollment import router as enrollment_router
from models.classroom import ClassGrade
from models.database import Base, Classroom, Student, User
from utils.auth import hash_password

SCHOOL_YEAR = 114
SEMESTER = 1


@pytest.fixture
def roster_client(tmp_path):
    db_path = tmp_path / "enrollment_roster_scope.sqlite"
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
    app.include_router(enrollment_router)

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


def _seed_class_with_student(session):
    grade = ClassGrade(name="大班", sort_order=1, is_active=True)
    session.add(grade)
    session.flush()
    cls = Classroom(
        name="向日葵",
        school_year=SCHOOL_YEAR,
        semester=SEMESTER,
        is_active=True,
        grade_id=grade.id,
    )
    session.add(cls)
    session.flush()
    stu = Student(
        student_id="S400",
        name="他班學生",
        classroom_id=cls.id,
        is_active=True,
        lifecycle_status="active",
        status_tag="新生",
    )
    session.add(stu)
    session.flush()


ROSTER_URL = (
    f"/api/student-enrollment/roster?school_year={SCHOOL_YEAR}&semester={SEMESTER}"
)


def test_own_class_scoped_role_sees_empty_roster(roster_client):
    """持 STUDENTS_READ:own_class 但無可存取班級者，roster 應過濾成空（不可讀全園）。"""
    client, sf = roster_client
    with sf() as s:
        _create_user(s, "principal_u", "principal", ["STUDENTS_READ:own_class"])
        _seed_class_with_student(s)
        s.commit()
    assert _login(client, "principal_u").status_code == 200
    res = client.get(ROSTER_URL)
    assert res.status_code == 200, res.text
    assert res.json()["classes"] == [], res.text


def test_admin_sees_full_roster(roster_client):
    """admin（unrestricted）看得到全園班級。"""
    client, sf = roster_client
    with sf() as s:
        _create_user(s, "admin_u", "admin", ["STUDENTS_READ"])
        _seed_class_with_student(s)
        s.commit()
    assert _login(client, "admin_u").status_code == 200
    res = client.get(ROSTER_URL)
    assert res.status_code == 200, res.text
    classes = res.json()["classes"]
    assert len(classes) == 1
    assert classes[0]["total"] == 1
