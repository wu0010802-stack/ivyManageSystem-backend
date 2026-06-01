"""medication_reminder_scheduler 安全網測試（兒童用藥安全；audit P1 #7）。

核心安全邏輯 `_active_orders_query`：今日 medication orders 必須**排除已核准請假
（覆蓋今日）的學生**——請假缺席的孩子若仍被列入餵藥提醒，會誤導老師/家長。
本檔鎖定此行為，避免日後 regression。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import (
    Base,
    Classroom,
    Student,
    User,
)
from models.portfolio import StudentMedicationOrder
from models.student_leave import StudentLeaveRequest
from services import medication_reminder_scheduler as sched

TODAY = date(2026, 5, 20)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    s = SessionFactory()
    # 共用前置：1 班、1 申請人 user、2 學生
    s.add(Classroom(id=1, name="兔兔班", is_active=True))
    s.add(
        User(
            id=1,
            username="teacher",
            password_hash="x",
            role="teacher",
            is_active=True,
            token_version=0,
        )
    )
    s.add(
        Student(
            id=1,
            student_id="S001",
            name="小明",
            classroom_id=1,
            lifecycle_status="active",
        )
    )
    s.add(
        Student(
            id=2,
            student_id="S002",
            name="小華",
            classroom_id=1,
            lifecycle_status="active",
        )
    )
    s.commit()
    yield s
    s.close()
    engine.dispose()


def _add_order(session, *, student_id: int, order_date: date = TODAY):
    session.add(
        StudentMedicationOrder(
            student_id=student_id,
            order_date=order_date,
            medication_name="退燒藥",
            dose="1 包",
            time_slots=["12:00"],
            source="teacher",
        )
    )
    session.commit()


def _add_leave(
    session, *, student_id: int, start: date, end: date, status: str = "approved"
):
    session.add(
        StudentLeaveRequest(
            student_id=student_id,
            applicant_user_id=1,
            leave_type="病假",
            start_date=start,
            end_date=end,
            status=status,
        )
    )
    session.commit()


def _ids(query):
    return {o.student_id for o in query.all()}


class TestActiveOrdersQuery:
    def test_today_order_without_leave_is_included(self, session):
        _add_order(session, student_id=1)
        assert _ids(sched._active_orders_query(session, TODAY)) == {1}

    def test_order_for_other_day_excluded(self, session):
        _add_order(session, student_id=1, order_date=TODAY - timedelta(days=1))
        assert _ids(sched._active_orders_query(session, TODAY)) == set()

    def test_student_on_approved_leave_today_is_excluded(self, session):
        """兒童安全關鍵：請假缺席學生不該被列入今日餵藥提醒。"""
        _add_order(session, student_id=1)
        _add_order(session, student_id=2)
        _add_leave(session, student_id=2, start=TODAY, end=TODAY)
        assert _ids(sched._active_orders_query(session, TODAY)) == {1}

    def test_leave_boundary_end_yesterday_included(self, session):
        _add_order(session, student_id=1)
        _add_leave(
            session,
            student_id=1,
            start=TODAY - timedelta(days=3),
            end=TODAY - timedelta(days=1),
        )
        assert _ids(sched._active_orders_query(session, TODAY)) == {1}

    def test_leave_boundary_start_tomorrow_included(self, session):
        _add_order(session, student_id=1)
        _add_leave(
            session,
            student_id=1,
            start=TODAY + timedelta(days=1),
            end=TODAY + timedelta(days=3),
        )
        assert _ids(sched._active_orders_query(session, TODAY)) == {1}

    def test_multi_day_leave_covering_today_excluded(self, session):
        _add_order(session, student_id=1)
        _add_leave(
            session,
            student_id=1,
            start=TODAY - timedelta(days=1),
            end=TODAY + timedelta(days=1),
        )
        assert _ids(sched._active_orders_query(session, TODAY)) == set()

    def test_non_approved_leave_does_not_exclude(self, session):
        """只有 approved 請假才排除；pending 請假不影響（避免漏餵藥）。"""
        _add_order(session, student_id=1)
        _add_leave(session, student_id=1, start=TODAY, end=TODAY, status="pending")
        assert _ids(sched._active_orders_query(session, TODAY)) == {1}


class TestCountAndRun:
    def test_count_today_medication_orders(self, session, monkeypatch):
        from contextlib import contextmanager

        _add_order(session, student_id=1)
        _add_order(session, student_id=2)
        _add_leave(session, student_id=2, start=TODAY, end=TODAY)

        @contextmanager
        def _scope():
            yield session

        monkeypatch.setattr(sched, "session_scope", _scope)
        # student 2 請假被排除 → 只剩 1
        assert sched.count_today_medication_orders(TODAY) == 1

    def test_run_medication_reminder_returns_order_count(self, session, monkeypatch):
        from contextlib import contextmanager

        _add_order(session, student_id=1)

        @contextmanager
        def _scope():
            yield session

        monkeypatch.setattr(sched, "session_scope", _scope)
        result = sched.run_medication_reminder(effective_date=TODAY)
        assert result["date"] == TODAY.isoformat()
        assert result["order_count"] == 1
        assert not result.get("skipped")
