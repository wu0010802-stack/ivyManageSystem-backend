"""GET /api/activity/attendance/sessions/{id}/roll.pdf 端點測試。"""

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
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    Base,
    Classroom,
    RegistrationCourse,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def activity_client(tmp_path):
    db_path = tmp_path / "activity-roll-pdf.sqlite"
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
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username: str, password: str, permissions: int) -> User:
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
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text
    return res


def _seed_session(session_factory):
    """建一個有 2 位學生 enrolled 的場次，回傳 session_id。"""
    with session_factory() as session:
        classroom = Classroom(name="向日葵班", is_active=True)
        session.add(classroom)
        session.flush()

        course = ActivityCourse(
            name="兒童瑜伽",
            price=1200,
            capacity=20,
            is_active=True,
        )
        session.add(course)
        session.flush()

        sess = ActivitySession(
            course_id=course.id,
            session_date=date(2026, 5, 22),
            notes="本日改至體能教室",
            created_by="test",
        )
        session.add(sess)
        session.flush()

        for name in ("王小明", "李小華"):
            reg = ActivityRegistration(
                student_name=name,
                birthday="2020-01-01",
                class_name="向日葵班",
                classroom_id=classroom.id,
                parent_phone="0912345678",
                is_active=True,
            )
            session.add(reg)
            session.flush()
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )

        session.commit()
        return sess.id


class TestAttendanceRollPdfEndpoint:
    def test_returns_pdf_200(self, activity_client):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_user(
                session,
                "act_admin",
                "TempPass123",
                Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            session.commit()

        session_id = _seed_session(session_factory)
        _login(client, "act_admin")

        res = client.get(f"/api/activity/attendance/sessions/{session_id}/roll.pdf")
        assert res.status_code == 200
        assert res.headers["content-type"] == "application/pdf"
        # PDF magic bytes
        assert res.content.startswith(b"%PDF-")
        # 至少幾 KB 表示有完整內容
        assert len(res.content) > 1500

    def test_forbidden_without_activity_read(self, activity_client):
        client, session_factory = activity_client

        with session_factory() as session:
            # 故意給一個沒有 ACTIVITY_READ 的權限位
            _create_user(session, "no_perm", "TempPass123", Permission.SALARY_READ)
            session.commit()

        session_id = _seed_session(session_factory)
        _login(client, "no_perm")

        res = client.get(f"/api/activity/attendance/sessions/{session_id}/roll.pdf")
        assert res.status_code == 403

    def test_not_found_when_session_missing(self, activity_client):
        client, session_factory = activity_client

        with session_factory() as session:
            _create_user(
                session,
                "act_admin",
                "TempPass123",
                Permission.ACTIVITY_READ,
            )
            session.commit()

        _login(client, "act_admin")

        res = client.get("/api/activity/attendance/sessions/99999/roll.pdf")
        assert res.status_code == 404
