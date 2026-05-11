"""tests/test_gov_moe_dashboard.py — 教育部 Dashboard Widget 整合測試。

涵蓋：
- 無資料時回傳空列表
- 只回傳到期日在時間窗內的學生
- 無 GOV_REPORTS_VIEW 權限時 403
"""

import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.gov_moe import router as gov_moe_router
from models.base import Base
from models.classroom import Student
from models.database import User
from models.gov_moe import StudentDisabilityDocument  # noqa: F401 — registers table
from utils.auth import hash_password
from utils.permissions import Permission

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gov_moe_client(tmp_path):
    db_path = tmp_path / "gov_moe_dashboard.sqlite"
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
    app.include_router(gov_moe_router, prefix="/api")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_admin(session_factory):
    with session_factory() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("AdminPass1"),
                role="admin",
                permissions=-1,
                is_active=True,
            )
        )
        s.commit()


def _seed_teacher(session_factory):
    with session_factory() as s:
        s.add(
            User(
                username="teacher",
                password_hash=hash_password("TeacherPass1"),
                role="teacher",
                permissions=int(Permission.DASHBOARD),
                is_active=True,
            )
        )
        s.commit()


def _login(client, username: str, password: str):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text
    token = res.json().get("access_token") or res.cookies.get("access_token")
    return token


@pytest.fixture
def authed_client_admin(gov_moe_client):
    client, sf = gov_moe_client
    _seed_admin(sf)
    token = _login(client, "admin", "AdminPass1")
    if token:
        client.headers.update({"Authorization": f"Bearer {token}"})
    return client, sf


@pytest.fixture
def authed_client_teacher(gov_moe_client):
    """Teacher client with separate DB (no GOV_REPORTS_VIEW permission)."""
    client, sf = gov_moe_client
    _seed_teacher(sf)
    token = _login(client, "teacher", "TeacherPass1")
    if token:
        client.headers.update({"Authorization": f"Bearer {token}"})
    return client, sf


@pytest.fixture
def db_session(authed_client_admin):
    """Return a session to the same DB the API uses for direct data seeding."""
    _, sf = authed_client_admin
    session = sf()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_disability_expiry_widget_no_data(authed_client_admin):
    client, _ = authed_client_admin
    res = client.get("/api/gov-moe/dashboard/disability-expiry?days=30")
    assert res.status_code == 200
    data = res.json()
    assert "total" in data
    assert "students" in data
    assert isinstance(data["students"], list)


def test_disability_expiry_widget_filters_within_window(
    authed_client_admin, db_session
):
    """Only students with disability_cert_expiry within [today, today+days] are listed."""
    client, _ = authed_client_admin

    in_window = Student(
        student_id="WIN001",
        name="即將到期",
        is_active=True,
        disability_cert_no="A1",
        disability_cert_expiry=date.today() + timedelta(days=15),
    )
    out_window = Student(
        student_id="OUT001",
        name="尚未到期",
        is_active=True,
        disability_cert_no="A2",
        disability_cert_expiry=date.today() + timedelta(days=90),
    )
    no_cert = Student(
        student_id="NOC001",
        name="無鑑定",
        is_active=True,
    )
    db_session.add_all([in_window, out_window, no_cert])
    db_session.commit()

    res = client.get("/api/gov-moe/dashboard/disability-expiry?days=30")
    assert res.status_code == 200
    data = res.json()
    names = {s["name"] for s in data["students"]}
    assert "即將到期" in names
    assert "尚未到期" not in names
    assert "無鑑定" not in names


def test_disability_expiry_widget_requires_permission(authed_client_teacher):
    client, _ = authed_client_teacher
    res = client.get("/api/gov-moe/dashboard/disability-expiry")
    assert res.status_code == 403
