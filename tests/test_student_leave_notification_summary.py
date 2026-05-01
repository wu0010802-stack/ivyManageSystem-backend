"""通知中心『家長新提交請假』區塊測試。"""

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
    Classroom,
    Student,
    StudentLeaveRequest,
    User,
)
from services.dashboard_query_service import dashboard_query_service
from utils.permissions import Permission


@pytest.fixture
def session(tmp_path):
    db_path = tmp_path / "n.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Session = sessionmaker(bind=engine)
    old_engine, old_factory = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = Session
    Base.metadata.create_all(engine)
    s = Session()
    yield s
    s.close()
    base_module._engine, base_module._SessionFactory = old_engine, old_factory
    engine.dispose()


def test_admin_sees_recent_parent_leave_count(session):
    classroom = Classroom(name="A", is_active=True)
    session.add(classroom)
    session.flush()
    student = Student(
        student_id="S1", name="小明", classroom_id=classroom.id, is_active=True
    )
    session.add(student)
    user = User(
        username="p", password_hash="!", role="parent", permissions=0, is_active=True
    )
    session.add(user)
    session.flush()
    today = date.today()
    # 5 天內 2 筆 / 8 天前 1 筆（不該被算入）
    session.add_all(
        [
            StudentLeaveRequest(
                student_id=student.id,
                applicant_user_id=user.id,
                leave_type="病假",
                start_date=today + timedelta(days=2),
                end_date=today + timedelta(days=2),
                status="approved",
                reviewed_at=datetime.now(),
                created_at=datetime.now() - timedelta(days=2),
            ),
            StudentLeaveRequest(
                student_id=student.id,
                applicant_user_id=user.id,
                leave_type="事假",
                start_date=today + timedelta(days=4),
                end_date=today + timedelta(days=4),
                status="approved",
                reviewed_at=datetime.now(),
                created_at=datetime.now() - timedelta(days=5),
            ),
            StudentLeaveRequest(
                student_id=student.id,
                applicant_user_id=user.id,
                leave_type="病假",
                start_date=today - timedelta(days=10),
                end_date=today - timedelta(days=10),
                status="approved",
                reviewed_at=datetime.now() - timedelta(days=8),
                created_at=datetime.now() - timedelta(days=8),
            ),
        ]
    )
    session.commit()

    dashboard_query_service._notification_cache.clear()

    summary = dashboard_query_service.build_notification_summary(
        session,
        user_permissions=Permission.STUDENTS_READ.value,
        current_user={"user_id": 999, "role": "admin", "permissions": -1},
    )
    items = [a for a in summary["action_items"] if a["type"] == "student_leave_recent"]
    assert len(items) == 1
    assert items[0]["count"] == 2
    assert items[0]["route"] == "/student-leaves"


def test_no_permission_no_block(session):
    dashboard_query_service._notification_cache.clear()
    summary = dashboard_query_service.build_notification_summary(
        session,
        user_permissions=0,
        current_user={"user_id": 999, "role": "admin", "permissions": 0},
    )
    items = [a for a in summary["action_items"] if a["type"] == "student_leave_recent"]
    assert items == []
