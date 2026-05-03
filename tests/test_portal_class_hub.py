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


from datetime import date
from services.portal_class_hub_service import count_attendance_pending


class TestCountAttendancePending:
    def test_no_records_means_all_pending(self, in_mem_session):
        sess = in_mem_session
        c = Classroom(name="A班", is_active=True)
        sess.add(c)
        sess.flush()
        for i in range(3):
            sess.add(
                Student(
                    student_id=f"S{i+1}",
                    name=f"小{i+1}",
                    classroom_id=c.id,
                    is_active=True,
                    lifecycle_status=LIFECYCLE_ACTIVE,
                )
            )
        sess.flush()
        assert (
            count_attendance_pending(sess, classroom_id=c.id, today=date(2026, 5, 4))
            == 3
        )

    def test_some_marked_some_pending(self, in_mem_session):
        from models.classroom import StudentAttendance

        sess = in_mem_session
        c = Classroom(name="B班", is_active=True)
        sess.add(c)
        sess.flush()
        students = []
        for i in range(3):
            s = Student(
                student_id=f"M{i+1}",
                name=f"中{i+1}",
                classroom_id=c.id,
                is_active=True,
                lifecycle_status=LIFECYCLE_ACTIVE,
            )
            sess.add(s)
            students.append(s)
        sess.flush()
        # 第 1 位已點名（出席）；第 2、3 位無 row
        sess.add(
            StudentAttendance(
                student_id=students[0].id,
                date=date(2026, 5, 4),
                status="出席",
            )
        )
        sess.flush()
        assert (
            count_attendance_pending(sess, classroom_id=c.id, today=date(2026, 5, 4))
            == 2
        )  # students[1] + students[2] (both no row)

    def test_inactive_students_excluded(self, in_mem_session):
        sess = in_mem_session
        c = Classroom(name="C班", is_active=True)
        sess.add(c)
        sess.flush()
        sess.add(
            Student(
                student_id="A1",
                name="active",
                classroom_id=c.id,
                is_active=True,
                lifecycle_status=LIFECYCLE_ACTIVE,
            )
        )
        sess.add(
            Student(
                student_id="I1",
                name="inactive",
                classroom_id=c.id,
                is_active=False,
                lifecycle_status=LIFECYCLE_ACTIVE,
            )
        )
        sess.flush()
        # 只有 active 學生計入 → 1
        assert (
            count_attendance_pending(sess, classroom_id=c.id, today=date(2026, 5, 4))
            == 1
        )

    def test_other_dates_not_counted(self, in_mem_session):
        from models.classroom import StudentAttendance

        sess = in_mem_session
        c = Classroom(name="D班", is_active=True)
        sess.add(c)
        sess.flush()
        s = Student(
            student_id="X1",
            name="x",
            classroom_id=c.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        sess.add(s)
        sess.flush()
        # 昨天有點名，今天無
        sess.add(
            StudentAttendance(
                student_id=s.id,
                date=date(2026, 5, 3),
                status="出席",
            )
        )
        sess.flush()
        assert (
            count_attendance_pending(sess, classroom_id=c.id, today=date(2026, 5, 4))
            == 1
        )


# ---------------------------------------------------------------------------
# Helper 5a: list_pending_medications
# ---------------------------------------------------------------------------
from services.portal_class_hub_service import list_pending_medications
from models.portfolio import StudentMedicationOrder, StudentMedicationLog


class TestListPendingMedications:
    def _make_classroom_student(self, sess, prefix="MED"):
        c = Classroom(name=f"{prefix}班", is_active=True)
        sess.add(c)
        sess.flush()
        s = Student(
            student_id=f"{prefix}001",
            name=f"{prefix}學生",
            classroom_id=c.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        sess.add(s)
        sess.flush()
        return c, s

    def test_returns_empty_when_no_orders(self, in_mem_session):
        sess = in_mem_session
        c, _s = self._make_classroom_student(sess)
        result = list_pending_medications(
            sess, classroom_id=c.id, today=date(2026, 5, 3)
        )
        assert result == []

    def test_returns_only_today_orders(self, in_mem_session):
        sess = in_mem_session
        c, s = self._make_classroom_student(sess, prefix="TOD")
        today = date(2026, 5, 3)
        yesterday = date(2026, 5, 2)
        # Yesterday's order + pending log — should NOT appear
        yesterday_order = StudentMedicationOrder(
            student_id=s.id,
            order_date=yesterday,
            medication_name="感冒藥",
            dose="1顆",
            time_slots=["09:00"],
        )
        sess.add(yesterday_order)
        sess.flush()
        yesterday_log = StudentMedicationLog(
            order_id=yesterday_order.id,
            scheduled_time="09:00",
            administered_at=None,
            skipped=False,
            correction_of=None,
        )
        sess.add(yesterday_log)
        # Today's order + pending log — should appear
        today_order = StudentMedicationOrder(
            student_id=s.id,
            order_date=today,
            medication_name="退燒藥",
            dose="5ml",
            time_slots=["10:00"],
        )
        sess.add(today_order)
        sess.flush()
        today_log = StudentMedicationLog(
            order_id=today_order.id,
            scheduled_time="10:00",
            administered_at=None,
            skipped=False,
            correction_of=None,
        )
        sess.add(today_log)
        sess.flush()
        result = list_pending_medications(sess, classroom_id=c.id, today=today)
        assert len(result) == 1
        assert result[0]["detail"] == "退燒藥 5ml"

    def test_skipped_and_administered_excluded(self, in_mem_session):
        from datetime import datetime as dt

        sess = in_mem_session
        c, s = self._make_classroom_student(sess, prefix="SKP")
        today = date(2026, 5, 3)
        order = StudentMedicationOrder(
            student_id=s.id,
            order_date=today,
            medication_name="維他命",
            dose="1顆",
            time_slots=["08:00", "12:00", "15:00"],
        )
        sess.add(order)
        sess.flush()
        # pending log (should appear)
        log_pending = StudentMedicationLog(
            order_id=order.id,
            scheduled_time="08:00",
            administered_at=None,
            skipped=False,
            correction_of=None,
        )
        # administered log (should NOT appear)
        log_administered = StudentMedicationLog(
            order_id=order.id,
            scheduled_time="12:00",
            administered_at=dt(2026, 5, 3, 12, 5),
            skipped=False,
            correction_of=None,
        )
        # skipped log (should NOT appear)
        log_skipped = StudentMedicationLog(
            order_id=order.id,
            scheduled_time="15:00",
            administered_at=None,
            skipped=True,
            correction_of=None,
        )
        sess.add_all([log_pending, log_administered, log_skipped])
        sess.flush()
        result = list_pending_medications(sess, classroom_id=c.id, today=today)
        assert len(result) == 1
        assert result[0]["id"] == log_pending.id

    def test_sorted_by_scheduled_time(self, in_mem_session):
        sess = in_mem_session
        c, s = self._make_classroom_student(sess, prefix="SRT")
        today = date(2026, 5, 3)
        order = StudentMedicationOrder(
            student_id=s.id,
            order_date=today,
            medication_name="止咳糖漿",
            dose="10ml",
            time_slots=["14:00", "08:30", "11:00"],
        )
        sess.add(order)
        sess.flush()
        logs = [
            StudentMedicationLog(
                order_id=order.id,
                scheduled_time="14:00",
                administered_at=None,
                skipped=False,
                correction_of=None,
            ),
            StudentMedicationLog(
                order_id=order.id,
                scheduled_time="08:30",
                administered_at=None,
                skipped=False,
                correction_of=None,
            ),
            StudentMedicationLog(
                order_id=order.id,
                scheduled_time="11:00",
                administered_at=None,
                skipped=False,
                correction_of=None,
            ),
        ]
        sess.add_all(logs)
        sess.flush()
        result = list_pending_medications(sess, classroom_id=c.id, today=today)
        assert len(result) == 3
        due_times = [r["due_at"] for r in result]
        assert due_times == sorted(due_times)
