"""學生出席總覽 API 回歸測試。"""

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
from api.student_attendance import router as student_attendance_router
from models.database import Base, Classroom, Student, StudentAttendance, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "student-attendance-api.sqlite"
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
    app.include_router(student_attendance_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username: str, permissions: int, password: str = "TempPass123") -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=permissions,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str, password: str = "TempPass123"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestStudentAttendanceOverviewApi:
    def test_overview_returns_totals_and_classrooms(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "student_admin", Permission.STUDENTS_READ)
            sun = Classroom(name="向日葵班", is_active=True)
            moon = Classroom(name="月亮班", is_active=True)
            hidden = Classroom(name="停用班", is_active=False)
            session.add_all([sun, moon, hidden])
            session.flush()

            active_sun = Student(student_id="S001", name="小明", classroom_id=sun.id, is_active=True)
            active_moon = Student(student_id="M001", name="小月", classroom_id=moon.id, is_active=True)
            inactive_student = Student(student_id="S999", name="已停用", classroom_id=sun.id, is_active=False)
            hidden_class_student = Student(student_id="H001", name="隱藏班學生", classroom_id=hidden.id, is_active=True)
            session.add_all([active_sun, active_moon, inactive_student, hidden_class_student])
            session.flush()

            session.add_all([
                StudentAttendance(student_id=active_sun.id, date=date(2026, 3, 12), status="出席"),
                StudentAttendance(student_id=active_moon.id, date=date(2026, 3, 12), status="病假"),
                StudentAttendance(student_id=hidden_class_student.id, date=date(2026, 3, 12), status="缺席"),
            ])
            session.commit()

        login_res = _login(client, "student_admin")
        assert login_res.status_code == 200

        res = client.get("/api/student-attendance/overview", params={"date": "2026-03-12"})
        assert res.status_code == 200

        data = res.json()
        assert data["date"] == "2026-03-12"
        assert data["totals"]["total_students"] == 2
        assert data["totals"]["recorded_count"] == 2
        assert data["totals"]["leave_count"] == 1
        assert len(data["classrooms"]) == 2

        sun_row = next(item for item in data["classrooms"] if item["classroom_name"] == "向日葵班")
        assert sun_row["student_count"] == 1
        assert sun_row["present_count"] == 1
        assert sun_row["rollcall_status"] == "complete"

        moon_row = next(item for item in data["classrooms"] if item["classroom_name"] == "月亮班")
        assert moon_row["student_count"] == 1
        assert moon_row["leave_count"] == 1
        assert moon_row["rollcall_status"] == "complete"

    def test_overview_requires_students_read_permission(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "student_forbidden", Permission.CLASSROOMS_READ)
            session.commit()

        login_res = _login(client, "student_forbidden")
        assert login_res.status_code == 200

        res = client.get("/api/student-attendance/overview", params={"date": "2026-03-12"})
        assert res.status_code == 403
