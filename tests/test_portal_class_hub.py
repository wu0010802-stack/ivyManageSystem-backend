"""教師工作台（class-hub）後端測試。"""

from __future__ import annotations

import os
import sys
from datetime import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime

from services.portal_class_hub_service import (
    SLOT_DEFINITIONS,
    classify_time_to_slot,
    pick_sticky_next,
)


class TestClassifyTimeToSlot:
    @pytest.mark.parametrize(
        "hh_mm,expected",
        [
            ("06:30", "morning"),  # 早於早晨 → 落入 morning
            ("07:00", "morning"),
            ("08:59", "morning"),
            ("09:00", "forenoon"),
            ("11:30", "forenoon"),
            ("12:00", "noon"),
            ("13:30", "noon"),
            ("14:00", "afternoon"),
            ("17:30", "afternoon"),
            ("19:00", "afternoon"),  # 晚於下午 → 落入 afternoon
        ],
    )
    def test_classify(self, hh_mm: str, expected: str):
        h, m = map(int, hh_mm.split(":"))
        assert classify_time_to_slot(time(h, m)) == expected


class TestPickStickyNext:
    def test_returns_earliest_future(self):
        now = datetime(2026, 5, 3, 10, 0)
        cands = [
            {"due_at": datetime(2026, 5, 3, 9, 0), "name": "past"},
            {"due_at": datetime(2026, 5, 3, 11, 0), "name": "soon"},
            {"due_at": datetime(2026, 5, 3, 14, 0), "name": "later"},
        ]
        assert pick_sticky_next(cands, now)["name"] == "soon"

    def test_returns_none_when_all_past(self):
        now = datetime(2026, 5, 3, 18, 0)
        cands = [{"due_at": datetime(2026, 5, 3, 9, 0)}]
        assert pick_sticky_next(cands, now) is None

    def test_returns_none_when_empty(self):
        assert pick_sticky_next([], datetime(2026, 5, 3, 10, 0)) is None


import models.base as base_module  # noqa: F401  (ensure mappers registered)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models.database import Base, Classroom, Employee, Student, User
from models.classroom import LIFECYCLE_ACTIVE
from services.portal_class_hub_service import resolve_teacher_classroom


@pytest.fixture
def in_mem_session(tmp_path):
    db_path = tmp_path / "hub.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    sess = sf()
    yield sess
    sess.close()
    engine.dispose()


class TestResolveTeacherClassroom:
    def test_returns_none_when_no_classroom(self, in_mem_session):
        sess = in_mem_session
        emp = Employee(employee_id="T001", name="老師A", is_active=True)
        sess.add(emp)
        sess.flush()
        assert resolve_teacher_classroom(sess, employee_id=emp.id) is None

    def test_returns_active_classroom_when_assigned(self, in_mem_session):
        sess = in_mem_session
        c = Classroom(name="A班", is_active=True)
        sess.add(c)
        sess.flush()
        emp = Employee(
            employee_id="T002", name="老師B", is_active=True, classroom_id=c.id
        )
        sess.add(emp)
        sess.flush()
        result = resolve_teacher_classroom(sess, employee_id=emp.id)
        assert result is not None
        assert result.id == c.id

    def test_returns_none_when_classroom_inactive(self, in_mem_session):
        sess = in_mem_session
        c = Classroom(name="C班", is_active=False)
        sess.add(c)
        sess.flush()
        emp = Employee(
            employee_id="T003", name="老師C", is_active=True, classroom_id=c.id
        )
        sess.add(emp)
        sess.flush()
        assert resolve_teacher_classroom(sess, employee_id=emp.id) is None

    def test_returns_none_when_employee_missing(self, in_mem_session):
        assert resolve_teacher_classroom(in_mem_session, employee_id=99999) is None
