"""GET /api/student-enrollment/roster.pdf 端點測試。"""

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
from models.classroom import ClassGrade, Classroom, Student
from models.database import Base, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def enrollment_client(tmp_path):
    db_path = tmp_path / "enrollment-roster-pdf.sqlite"
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


def _create_user(session, username, password, permissions):
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


def _login(client, username, password="TempPass123"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


def _seed_roster(session_factory):
    """建一個有 1 班 2 生的學期，回傳 (school_year, semester)."""
    with session_factory() as session:
        grade = ClassGrade(name="小班", sort_order=1)
        session.add(grade)
        session.flush()

        classroom = Classroom(
            name="向日葵班",
            grade_id=grade.id,
            school_year=114,
            semester=1,
            is_active=True,
        )
        session.add(classroom)
        session.flush()

        for sid, name, tag in (
            ("S001", "王小明", None),
            ("S002", "李小華", "新生"),
        ):
            session.add(
                Student(
                    student_id=sid,
                    name=name,
                    classroom_id=classroom.id,
                    is_active=True,
                    status_tag=tag,
                )
            )
        session.commit()
    return 114, 1


class TestEnrollmentRosterPdfEndpoint:
    def test_returns_pdf_200(self, enrollment_client):
        client, session_factory = enrollment_client
        with session_factory() as session:
            _create_user(session, "stu_admin", "TempPass123", Permission.STUDENTS_READ)
            session.commit()
        sy, sem = _seed_roster(session_factory)
        _login(client, "stu_admin")

        res = client.get(
            "/api/student-enrollment/roster.pdf",
            params={"school_year": sy, "semester": sem},
        )
        assert res.status_code == 200, res.text
        assert res.headers["content-type"] == "application/pdf"
        assert res.content.startswith(b"%PDF-")
        assert len(res.content) > 1500

    def test_forbidden_without_students_read(self, enrollment_client):
        client, session_factory = enrollment_client
        with session_factory() as session:
            _create_user(session, "no_perm", "TempPass123", Permission.SALARY_READ)
            session.commit()
        sy, sem = _seed_roster(session_factory)
        _login(client, "no_perm")

        res = client.get(
            "/api/student-enrollment/roster.pdf",
            params={"school_year": sy, "semester": sem},
        )
        assert res.status_code == 403

    def test_empty_term_still_returns_valid_pdf(self, enrollment_client):
        """無班級的學期也應回 200 + 有效 PDF（不該爆）。"""
        client, session_factory = enrollment_client
        with session_factory() as session:
            _create_user(session, "stu_admin", "TempPass123", Permission.STUDENTS_READ)
            session.commit()
        _login(client, "stu_admin")

        res = client.get(
            "/api/student-enrollment/roster.pdf",
            params={"school_year": 999, "semester": 1},
        )
        assert res.status_code == 200
        assert res.content.startswith(b"%PDF-")
