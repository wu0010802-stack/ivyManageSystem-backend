"""後台通知中心聚合 API 測試。"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.notifications import router as notifications_router
from models.database import (
    Base,
    Employee,
    LeaveRecord,
    OvertimeRecord,
    ParentInquiry,
    PunchCorrectionRequest,
    SchoolEvent,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def notification_client(tmp_path):
    """建立隔離 sqlite 測試 app。"""
    db_path = tmp_path / "notifications.sqlite"
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
    app.include_router(notifications_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_admin(session, *, username="notify_admin", password="TempPass123", permissions=0) -> User:
    admin = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=permissions,
        is_active=True,
    )
    session.add(admin)
    session.flush()
    return admin


def _create_employee(session, *, employee_id: str, name: str, probation_end_date=None) -> Employee:
    employee = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=32000,
        is_active=True,
        probation_end_date=probation_end_date,
    )
    session.add(employee)
    session.flush()
    return employee


def _login(client: TestClient, username="notify_admin", password="TempPass123"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestNotificationSummary:
    def test_summary_aggregates_action_items_and_reminders_by_permission(self, notification_client):
        client, session_factory = notification_client
        today = date.today()
        next_month = date(today.year + 1, 1, 10) if today.month == 12 else date(today.year, today.month + 1, 10)

        with session_factory() as session:
            _create_admin(
                session,
                permissions=(
                    Permission.APPROVALS
                    | Permission.ACTIVITY_READ
                    | Permission.CALENDAR
                    | Permission.EMPLOYEES_READ
                ),
            )
            employee = _create_employee(session, employee_id="E001", name="王小明", probation_end_date=next_month)
            session.add(
                LeaveRecord(
                    employee_id=employee.id,
                    leave_type="personal",
                    start_date=today,
                    end_date=today,
                    leave_hours=8,
                    is_approved=None,
                )
            )
            session.add(
                OvertimeRecord(
                    employee_id=employee.id,
                    overtime_date=today,
                    overtime_type="weekday",
                    start_time=datetime.combine(today, datetime.min.time()),
                    end_time=datetime.combine(today, datetime.min.time()) + timedelta(hours=2),
                    hours=2,
                    is_approved=None,
                )
            )
            session.add(
                PunchCorrectionRequest(
                    employee_id=employee.id,
                    attendance_date=today,
                    correction_type="punch_in",
                    requested_punch_in=datetime.combine(today, datetime.min.time()),
                    is_approved=None,
                )
            )
            session.add(ParentInquiry(name="家長甲", phone="0912", question="想詢問上課時間", is_read=False))
            session.add(
                SchoolEvent(
                    title="親師座談",
                    event_date=today + timedelta(days=2),
                    event_type="meeting",
                    is_active=True,
                )
            )
            session.commit()

        login_res = _login(client)
        assert login_res.status_code == 200

        res = client.get("/api/notifications/summary")

        assert res.status_code == 200
        data = res.json()
        assert data["total_badge"] == 4

        action_items = {item["type"]: item for item in data["action_items"]}
        assert set(action_items) == {"approval", "activity_inquiry"}
        assert action_items["approval"]["count"] == 3
        assert action_items["approval"]["route"] == "/approvals"
        assert action_items["approval"]["breakdown"] == {
            "leaves": 1,
            "overtimes": 1,
            "punch_corrections": 1,
        }
        assert action_items["activity_inquiry"]["count"] == 1
        assert action_items["activity_inquiry"]["route"] == "/activity/inquiries"

        reminders = {item["type"]: item for item in data["reminders"]}
        assert set(reminders) == {"calendar", "probation"}
        assert reminders["calendar"]["route"] == "/calendar"
        assert reminders["calendar"]["items"][0]["label"] == "親師座談"
        assert reminders["probation"]["route"] == "/employees"
        assert reminders["probation"]["items"][0]["label"] == "E001 王小明"

    def test_summary_hides_sections_without_permission(self, notification_client):
        client, session_factory = notification_client
        today = date.today()

        with session_factory() as session:
            _create_admin(session, username="calendar_only", permissions=Permission.CALENDAR)
            session.add(
                SchoolEvent(
                    title="校務活動",
                    event_date=today + timedelta(days=1),
                    event_type="activity",
                    is_active=True,
                )
            )
            session.add(ParentInquiry(name="家長乙", phone="0922", question="未授權不應看見", is_read=False))
            session.commit()

        login_res = _login(client, username="calendar_only")
        assert login_res.status_code == 200

        res = client.get("/api/notifications/summary")

        assert res.status_code == 200
        data = res.json()
        assert data["total_badge"] == 0
        assert data["action_items"] == []
        assert [item["type"] for item in data["reminders"]] == ["calendar"]

    def test_summary_returns_empty_arrays_when_no_notifications(self, notification_client):
        client, session_factory = notification_client

        with session_factory() as session:
            _create_admin(
                session,
                username="empty_user",
                permissions=(
                    Permission.APPROVALS
                    | Permission.ACTIVITY_READ
                    | Permission.CALENDAR
                    | Permission.EMPLOYEES_READ
                ),
            )
            session.commit()

        login_res = _login(client, username="empty_user")
        assert login_res.status_code == 200

        res = client.get("/api/notifications/summary")

        assert res.status_code == 200
        assert res.json() == {
            "total_badge": 0,
            "action_items": [],
            "reminders": [],
        }
