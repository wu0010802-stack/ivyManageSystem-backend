"""Dashboard / notification query service tests."""

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    Base,
    Employee,
    LeaveRecord,
    OvertimeRecord,
    ParentInquiry,
    PunchCorrectionRequest,
    SchoolEvent,
)
from services.dashboard_query_service import DashboardQueryService
from utils.permissions import Permission


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "dashboard-query.sqlite"
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

    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        base_module._engine = old_engine
        base_module._SessionFactory = old_session_factory
        engine.dispose()


def _create_employee(session, *, employee_id: str, name: str) -> Employee:
    employee = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=32000,
        is_active=True,
    )
    session.add(employee)
    session.flush()
    return employee


class TestDashboardQueryService:
    def test_notification_summary_aggregates_sections_by_permission(self, db_session):
        service = DashboardQueryService()
        today = date.today()

        employee = _create_employee(db_session, employee_id="E001", name="王小明")
        db_session.add(
            LeaveRecord(
                employee_id=employee.id,
                leave_type="personal",
                start_date=today,
                end_date=today,
                leave_hours=8,
                is_approved=None,
            )
        )
        db_session.add(
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
        db_session.add(
            PunchCorrectionRequest(
                employee_id=employee.id,
                attendance_date=today,
                correction_type="punch_in",
                requested_punch_in=datetime.combine(today, datetime.min.time()),
                is_approved=None,
            )
        )
        db_session.add(ParentInquiry(name="家長甲", phone="0912", question="想詢問上課時間", is_read=False))
        db_session.add(
            SchoolEvent(
                title="親師座談",
                event_date=today + timedelta(days=2),
                event_type="meeting",
                is_active=True,
            )
        )
        db_session.commit()

        summary = service.build_notification_summary(
            db_session,
            user_permissions=(
                Permission.APPROVALS
                | Permission.ACTIVITY_READ
                | Permission.CALENDAR
                | Permission.EMPLOYEES_READ
            ),
        )

        assert summary["total_badge"] == 4
        action_items = {item["type"]: item for item in summary["action_items"]}
        assert action_items["approval"]["count"] == 3
        assert action_items["activity_inquiry"]["count"] == 1

        reminders = {item["type"]: item for item in summary["reminders"]}
        assert reminders["calendar"]["items"][0]["label"] == "親師座談"

    def test_home_sections_only_include_permitted_queries(self, db_session, monkeypatch):
        service = DashboardQueryService()
        calls = []

        monkeypatch.setattr(service, "build_approval_summary", lambda session, today=None: calls.append("approval") or {"total": 1})
        monkeypatch.setattr(service, "build_upcoming_events", lambda session, days=7, today=None: calls.append("events") or [])
        monkeypatch.setattr(service, "build_student_attendance_summary", lambda session, today=None: calls.append("student") or {"total_students": 0})
        monkeypatch.setattr(service, "build_activity_stats", lambda session: calls.append("activity") or {"statistics": {}})

        sections = service.build_home_sections(
            db_session,
            user_permissions=Permission.APPROVALS | Permission.STUDENTS_READ | Permission.ACTIVITY_READ,
        )

        assert calls == ["approval", "student", "activity"]
        assert set(sections) == {"approval_summary", "student_attendance_summary", "activity_stats"}
