"""GET /api/portal/attendance-sheet.pdf 端點測試。"""

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
from api.portal.attendance import router as portal_attendance_router
from models.database import Base, Employee, User
from utils.auth import hash_password


@pytest.fixture
def portal_client(tmp_path):
    db_path = tmp_path / "portal-attendance-pdf.sqlite"
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
    app.include_router(portal_attendance_router, prefix="/api/portal")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_employee_user(session, username, name="王教師"):
    emp = Employee(
        employee_id="T001",
        name=name,
        base_salary=32000,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    user = User(
        username=username,
        password_hash=hash_password("TempPass123"),
        role="teacher",
        employee_id=emp.id,
        permissions=0,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user, emp


def _login(client, username, password="TempPass123"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


class TestPortalAttendanceSheetPdf:
    def test_returns_pdf_200(self, portal_client):
        client, sf = portal_client
        with sf() as s:
            _create_employee_user(s, "teacher_a")
            s.commit()
        _login(client, "teacher_a")

        res = client.get(
            "/api/portal/attendance-sheet.pdf",
            params={"year": 2026, "month": 5},
        )
        assert res.status_code == 200, res.text
        assert res.headers["content-type"] == "application/pdf"
        assert res.content.startswith(b"%PDF-")
        assert len(res.content) > 1500

    def test_unauthenticated_401(self, portal_client):
        client, _sf = portal_client
        res = client.get(
            "/api/portal/attendance-sheet.pdf",
            params={"year": 2026, "month": 5},
        )
        # 未登入應該 401（依 portal auth dependency）
        assert res.status_code in (401, 403)

    def test_invalid_month_422(self, portal_client):
        client, sf = portal_client
        with sf() as s:
            _create_employee_user(s, "teacher_b")
            s.commit()
        _login(client, "teacher_b")

        res = client.get(
            "/api/portal/attendance-sheet.pdf",
            params={"year": 2026, "month": 13},
        )
        assert res.status_code == 422
