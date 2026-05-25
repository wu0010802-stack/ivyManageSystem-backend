"""academic-summary endpoint 測試。

驗證 4 指標計算、學期區間、權限守衛、無資料 fallback。
"""

import os
import sys
from datetime import date, datetime

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
from models.database import (
    Base,
    Classroom,
    Student,
    StudentAssessment,
    StudentAttendance,
    StudentIncident,
    StudentLeaveRequest,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "academic-summary.sqlite"
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


def _create_user(session, username, perms, password="TempPass123") -> User:
    if isinstance(perms, str):
        perms = [perms]
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permission_names=perms,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password="TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


class TestAcademicSummary:
    def test_summary_computes_four_metrics_for_semester(self, client_with_db):
        """sy=114 sem=2 → 2026-02-01 ~ 2026-07-31。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "admin1", Permission.STUDENTS_READ)
            cls = Classroom(name="A", is_active=True)
            session.add(cls)
            session.flush()
            stu = Student(
                student_id="S001", name="阿明", classroom_id=cls.id, is_active=True
            )
            session.add(stu)
            session.flush()
            sid = stu.id

            # 區間內：3 筆出席（2 出席、1 病假） + 區間外 1 筆（不計）
            session.add_all(
                [
                    StudentAttendance(
                        student_id=sid, date=date(2026, 3, 1), status="出席"
                    ),
                    StudentAttendance(
                        student_id=sid, date=date(2026, 3, 2), status="出席"
                    ),
                    StudentAttendance(
                        student_id=sid, date=date(2026, 3, 3), status="病假"
                    ),
                    StudentAttendance(
                        student_id=sid, date=date(2025, 12, 1), status="出席"
                    ),
                ]
            )

            # 請假：approved 3 天（3/3~3/5）、rejected 不算
            session.add(
                StudentLeaveRequest(
                    student_id=sid,
                    applicant_user_id=1,
                    leave_type="病假",
                    start_date=date(2026, 3, 3),
                    end_date=date(2026, 3, 5),
                    status="approved",
                )
            )
            session.add(
                StudentLeaveRequest(
                    student_id=sid,
                    applicant_user_id=1,
                    leave_type="事假",
                    start_date=date(2026, 4, 1),
                    end_date=date(2026, 4, 1),
                    status="rejected",
                )
            )

            session.add_all(
                [
                    StudentAssessment(
                        student_id=sid,
                        semester="2025下",
                        assessment_type="期中",
                        content="x",
                        assessment_date=date(2026, 3, 15),
                    ),
                    StudentAssessment(
                        student_id=sid,
                        semester="2025下",
                        assessment_type="期末",
                        content="x",
                        assessment_date=date(2026, 6, 15),
                    ),
                ]
            )
            session.add(
                StudentIncident(
                    student_id=sid,
                    incident_type="意外受傷",
                    occurred_at=datetime(2026, 4, 10, 10, 0),
                    description="x",
                )
            )
            session.commit()

        assert _login(client, "admin1").status_code == 200
        res = client.get(
            f"/api/students/{sid}/academic-summary",
            params={"school_year": 114, "semester": 2},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["school_year"] == 114
        assert data["semester"] == 2
        assert data["period"]["from"] == "2026-02-01"
        assert data["period"]["to"] == "2026-07-31"
        assert data["attendance_total"] == 3
        assert data["attendance_present"] == 2
        assert abs(data["attendance_rate"] - 2 / 3) < 0.001
        assert data["leave_days"] == 3
        assert data["assessment_count"] == 2
        assert data["incident_count"] == 1

    def test_summary_zero_for_no_data(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "admin1", Permission.STUDENTS_READ)
            cls = Classroom(name="B", is_active=True)
            session.add(cls)
            session.flush()
            stu = Student(
                student_id="S002", name="小華", classroom_id=cls.id, is_active=True
            )
            session.add(stu)
            session.flush()
            sid = stu.id
            session.commit()

        assert _login(client, "admin1").status_code == 200
        res = client.get(
            f"/api/students/{sid}/academic-summary",
            params={"school_year": 114, "semester": 2},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["attendance_rate"] == 0.0
        assert data["leave_days"] == 0
        assert data["assessment_count"] == 0
        assert data["incident_count"] == 0

    def test_summary_requires_students_read_permission(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "no_perm", Permission.CLASSROOMS_READ)
            cls = Classroom(name="C", is_active=True)
            session.add(cls)
            session.flush()
            stu = Student(
                student_id="S003", name="禁區", classroom_id=cls.id, is_active=True
            )
            session.add(stu)
            session.flush()
            sid = stu.id
            session.commit()

        assert _login(client, "no_perm").status_code == 200
        res = client.get(
            f"/api/students/{sid}/academic-summary",
            params={"school_year": 114, "semester": 2},
        )
        assert res.status_code == 403

    def test_summary_partial_semester_filter_returns_400(self, client_with_db):
        """只帶 school_year 不帶 semester（或反之）應 400。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "admin1", Permission.STUDENTS_READ)
            cls = Classroom(name="D", is_active=True)
            session.add(cls)
            session.flush()
            stu = Student(
                student_id="S004", name="x", classroom_id=cls.id, is_active=True
            )
            session.add(stu)
            session.flush()
            sid = stu.id
            session.commit()

        assert _login(client, "admin1").status_code == 200
        res = client.get(
            f"/api/students/{sid}/academic-summary",
            params={"school_year": 114},
        )
        assert res.status_code == 400
