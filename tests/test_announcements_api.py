"""公告管理 API 回歸測試。"""

import os
import sys
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.announcements import router as announcements_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import Announcement, AnnouncementRead, Base, Employee, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def announcements_client(tmp_path):
    db_path = tmp_path / "announcements-api.sqlite"
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
    app.include_router(announcements_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username: str, employee_id: int) -> User:
    user = User(
        username=username,
        password_hash=hash_password("TempPass123"),
        role="admin",
        permissions=Permission.ANNOUNCEMENTS_READ | Permission.ANNOUNCEMENTS_WRITE,
        employee_id=employee_id,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _create_employee(session, employee_id: str, name: str) -> Employee:
    employee = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=32000,
        is_active=True,
    )
    session.add(employee)
    session.flush()
    return employee


def _login(client: TestClient, username: str):
    return client.post("/api/auth/login", json={"username": username, "password": "TempPass123"})


class TestAnnouncementsApi:
    def test_list_announcements_returns_read_preview_and_full_reader_list(self, announcements_client):
        client, session_factory = announcements_client
        with session_factory() as session:
            author = _create_employee(session, "E001", "園長")
            reader_a = _create_employee(session, "E002", "王老師")
            reader_b = _create_employee(session, "E003", "林老師")
            reader_c = _create_employee(session, "E004", "陳老師")
            reader_d = _create_employee(session, "E005", "黃老師")
            _create_user(session, "announcement_admin", author.id)

            announcement = Announcement(
                title="校務提醒",
                content="請確認明天活動流程",
                priority="important",
                is_pinned=True,
                created_by=author.id,
            )
            session.add(announcement)
            session.flush()

            base_time = datetime(2026, 3, 14, 9, 0, 0)
            session.add_all([
                AnnouncementRead(announcement_id=announcement.id, employee_id=reader_a.id, read_at=base_time),
                AnnouncementRead(announcement_id=announcement.id, employee_id=reader_b.id, read_at=base_time + timedelta(minutes=5)),
                AnnouncementRead(announcement_id=announcement.id, employee_id=reader_c.id, read_at=base_time + timedelta(minutes=10)),
                AnnouncementRead(announcement_id=announcement.id, employee_id=reader_d.id, read_at=base_time + timedelta(minutes=15)),
            ])
            session.commit()

        login_res = _login(client, "announcement_admin")
        assert login_res.status_code == 200

        res = client.get("/api/announcements")

        assert res.status_code == 200
        payload = res.json()["items"][0]
        assert payload["read_count"] == 4
        assert [reader["name"] for reader in payload["read_preview"]] == ["黃老師", "陳老師", "林老師"]
        assert payload["has_more_readers"] is True
        assert [reader["name"] for reader in payload["readers"]] == ["黃老師", "陳老師", "林老師", "王老師"]
