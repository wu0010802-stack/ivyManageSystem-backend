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


# ---------------------------------------------------------------------------
# Helper 5b: count_observation_pending
# ---------------------------------------------------------------------------
from datetime import datetime as _dt


class TestCountObservationPending:
    def test_no_records_means_all_pending(self, in_mem_session):
        from services.portal_class_hub_service import count_observation_pending

        sess = in_mem_session
        c = Classroom(name="O班", is_active=True)
        sess.add(c)
        sess.flush()
        for i in range(3):
            sess.add(
                Student(
                    student_id=f"O{i+1}",
                    name=f"obs{i+1}",
                    classroom_id=c.id,
                    is_active=True,
                    lifecycle_status=LIFECYCLE_ACTIVE,
                )
            )
        sess.flush()
        assert (
            count_observation_pending(sess, classroom_id=c.id, today=date(2026, 5, 4))
            == 3
        )

    def test_one_recorded_per_student(self, in_mem_session):
        from services.portal_class_hub_service import count_observation_pending
        from models.portfolio import StudentObservation

        sess = in_mem_session
        c = Classroom(name="O2班", is_active=True)
        sess.add(c)
        sess.flush()
        students = []
        for i in range(3):
            s = Student(
                student_id=f"P{i+1}",
                name=f"p{i+1}",
                classroom_id=c.id,
                is_active=True,
                lifecycle_status=LIFECYCLE_ACTIVE,
            )
            sess.add(s)
            students.append(s)
        sess.flush()
        # student[0] 有 2 筆觀察、student[1] 有 1 筆、student[2] 無
        sess.add(
            StudentObservation(
                student_id=students[0].id,
                observation_date=date(2026, 5, 4),
                narrative="obs A1",
            )
        )
        sess.add(
            StudentObservation(
                student_id=students[0].id,
                observation_date=date(2026, 5, 4),
                narrative="obs A2",
            )
        )
        sess.add(
            StudentObservation(
                student_id=students[1].id,
                observation_date=date(2026, 5, 4),
                narrative="obs B",
            )
        )
        sess.flush()
        assert (
            count_observation_pending(sess, classroom_id=c.id, today=date(2026, 5, 4))
            == 1
        )  # 只剩 student[2]

    def test_deleted_observation_still_pending(self, in_mem_session):
        from services.portal_class_hub_service import count_observation_pending
        from models.portfolio import StudentObservation

        sess = in_mem_session
        c = Classroom(name="O3班", is_active=True)
        sess.add(c)
        sess.flush()
        s = Student(
            student_id="D1",
            name="deleted-obs-stu",
            classroom_id=c.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        sess.add(s)
        sess.flush()
        sess.add(
            StudentObservation(
                student_id=s.id,
                observation_date=date(2026, 5, 4),
                narrative="will be soft-deleted",
                deleted_at=_dt(2026, 5, 4, 10, 0),
            )
        )
        sess.flush()
        assert (
            count_observation_pending(sess, classroom_id=c.id, today=date(2026, 5, 4))
            == 1
        )


# ---------------------------------------------------------------------------
# Helper 5c: count_incidents_today
# ---------------------------------------------------------------------------
from models.classroom import StudentIncident


class TestCountIncidentsToday:
    def test_counts_only_today(self, in_mem_session):
        from services.portal_class_hub_service import count_incidents_today

        sess = in_mem_session
        c = Classroom(name="I班", is_active=True)
        sess.add(c)
        sess.flush()
        s = Student(
            student_id="X",
            name="x",
            classroom_id=c.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        sess.add(s)
        sess.flush()
        # 2 today + 1 yesterday
        sess.add(
            StudentIncident(
                student_id=s.id,
                incident_type="意外受傷",
                occurred_at=_dt(2026, 5, 4, 9, 0),
                description="early today",
            )
        )
        sess.add(
            StudentIncident(
                student_id=s.id,
                incident_type="行為觀察",
                occurred_at=_dt(2026, 5, 4, 23, 30),
                description="late today",
            )
        )
        sess.add(
            StudentIncident(
                student_id=s.id,
                incident_type="意外受傷",
                occurred_at=_dt(2026, 5, 3, 15, 0),
                description="yesterday",
            )
        )
        sess.flush()
        assert (
            count_incidents_today(sess, classroom_id=c.id, today=date(2026, 5, 4)) == 2
        )

    def test_filters_by_classroom(self, in_mem_session):
        from services.portal_class_hub_service import count_incidents_today

        sess = in_mem_session
        c1 = Classroom(name="C1", is_active=True)
        c2 = Classroom(name="C2", is_active=True)
        sess.add_all([c1, c2])
        sess.flush()
        s1 = Student(
            student_id="S1",
            name="s1",
            classroom_id=c1.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        s2 = Student(
            student_id="S2",
            name="s2",
            classroom_id=c2.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        sess.add_all([s1, s2])
        sess.flush()
        sess.add(
            StudentIncident(
                student_id=s1.id,
                incident_type="意外受傷",
                occurred_at=_dt(2026, 5, 4, 9, 0),
                description="c1 incident",
            )
        )
        sess.add(
            StudentIncident(
                student_id=s2.id,
                incident_type="意外受傷",
                occurred_at=_dt(2026, 5, 4, 9, 0),
                description="c2 incident",
            )
        )
        sess.flush()
        assert (
            count_incidents_today(sess, classroom_id=c1.id, today=date(2026, 5, 4)) == 1
        )
        assert (
            count_incidents_today(sess, classroom_id=c2.id, today=date(2026, 5, 4)) == 1
        )


# ---------------------------------------------------------------------------
# Helper 5d: count_contact_book_pending
# ---------------------------------------------------------------------------


class TestCountContactBookPending:
    def test_no_entries_all_pending(self, in_mem_session):
        from services.portal_class_hub_service import count_contact_book_pending

        sess = in_mem_session
        c = Classroom(name="CB", is_active=True)
        sess.add(c)
        sess.flush()
        for i in range(2):
            sess.add(
                Student(
                    student_id=f"CB{i+1}",
                    name=f"cb{i+1}",
                    classroom_id=c.id,
                    is_active=True,
                    lifecycle_status=LIFECYCLE_ACTIVE,
                )
            )
        sess.flush()
        assert (
            count_contact_book_pending(sess, classroom_id=c.id, today=date(2026, 5, 4))
            == 2
        )

    def test_some_filled_some_not(self, in_mem_session):
        from services.portal_class_hub_service import count_contact_book_pending
        from models.contact_book import StudentContactBookEntry

        sess = in_mem_session
        c = Classroom(name="CB2", is_active=True)
        sess.add(c)
        sess.flush()
        students = []
        for i in range(3):
            s = Student(
                student_id=f"E{i+1}",
                name=f"e{i+1}",
                classroom_id=c.id,
                is_active=True,
                lifecycle_status=LIFECYCLE_ACTIVE,
            )
            sess.add(s)
            students.append(s)
        sess.flush()
        sess.add(
            StudentContactBookEntry(
                student_id=students[0].id,
                classroom_id=c.id,
                log_date=date(2026, 5, 4),
                teacher_note="filled",
                published_at=_dt(2026, 5, 4, 17, 0),
            )
        )
        sess.flush()
        assert (
            count_contact_book_pending(sess, classroom_id=c.id, today=date(2026, 5, 4))
            == 2
        )

    def test_draft_counts_as_filled(self, in_mem_session):
        from services.portal_class_hub_service import count_contact_book_pending
        from models.contact_book import StudentContactBookEntry

        sess = in_mem_session
        c = Classroom(name="CB3", is_active=True)
        sess.add(c)
        sess.flush()
        s = Student(
            student_id="D2",
            name="draft-stu",
            classroom_id=c.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        sess.add(s)
        sess.flush()
        sess.add(
            StudentContactBookEntry(
                student_id=s.id,
                classroom_id=c.id,
                log_date=date(2026, 5, 4),
                teacher_note="draft",
                published_at=None,
            )
        )
        sess.flush()
        assert (
            count_contact_book_pending(sess, classroom_id=c.id, today=date(2026, 5, 4))
            == 0
        )


# ---------------------------------------------------------------------------
# 整合測試：GET /api/portal/class-hub/today
# ---------------------------------------------------------------------------
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.portal import router as portal_router
from utils.auth import create_access_token


@pytest.fixture
def hub_client(in_mem_session):
    """整合測試 client：把 SQLite session 注入 FastAPI。"""
    sess = in_mem_session
    app = FastAPI()
    app.include_router(portal_router)
    from models.database import get_session as real_get_session

    def override():
        try:
            yield sess
        finally:
            pass

    app.dependency_overrides[real_get_session] = override
    return TestClient(app), sess


def _create_user(sess, *, employee_id, username, role="teacher"):
    u = User(
        username=username,
        password_hash="x",
        role=role,
        employee_id=employee_id,
        is_active=True,
    )
    sess.add(u)
    sess.flush()
    return u


class TestClassHubTodayEndpoint:
    def test_no_classroom_returns_empty_shell(self, hub_client):
        c, sess = hub_client
        emp = Employee(name="無班老師", is_active=True, employee_id="E0")
        sess.add(emp)
        sess.flush()
        u = _create_user(sess, employee_id=emp.id, username="t1")
        sess.commit()
        token = create_access_token({"sub": u.username, "employee_id": emp.id})
        resp = c.get(
            "/api/portal/class-hub/today",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["classroom_id"] == 0
        assert body["classroom_name"] == ""
        assert body["counts"]["attendance_pending"] == 0
        assert body["counts"]["medications_pending"] == 0
        assert len(body["slots"]) == 4
        assert {s["slot_id"] for s in body["slots"]} == {
            "morning",
            "forenoon",
            "noon",
            "afternoon",
        }
        assert all(s["tasks"] == [] for s in body["slots"])
        assert body["sticky_next"] is None

    def test_with_classroom_attendance_only(self, hub_client):
        c, sess = hub_client
        room = Classroom(name="C班", is_active=True)
        sess.add(room)
        sess.flush()
        for i in range(3):
            sess.add(
                Student(
                    student_id=f"H{i+1}",
                    name=f"happy{i+1}",
                    classroom_id=room.id,
                    is_active=True,
                    lifecycle_status=LIFECYCLE_ACTIVE,
                )
            )
        emp = Employee(
            name="班導", is_active=True, classroom_id=room.id, employee_id="E1"
        )
        sess.add(emp)
        sess.flush()
        u = _create_user(sess, employee_id=emp.id, username="t2")
        sess.commit()
        token = create_access_token(
            {"sub": u.username, "employee_id": emp.id, "permissions": -1}
        )
        resp = c.get(
            "/api/portal/class-hub/today",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["classroom_id"] == room.id
        assert body["classroom_name"] == "C班"
        assert body["counts"]["attendance_pending"] == 3
        morning = next(s for s in body["slots"] if s["slot_id"] == "morning")
        kinds = {t["kind"] for t in morning["tasks"]}
        assert "attendance" in kinds
        # forenoon should still have at least the incident (count=0) inline_button
        forenoon = next(s for s in body["slots"] if s["slot_id"] == "forenoon")
        assert any(t["kind"] == "incident" for t in forenoon["tasks"])

    def test_medication_sets_sticky_and_correct_slot(self, hub_client):
        from models.portfolio import StudentMedicationOrder, StudentMedicationLog

        c, sess = hub_client
        room = Classroom(name="M班", is_active=True)
        sess.add(room)
        sess.flush()
        s = Student(
            student_id="MS1",
            name="med-stu",
            classroom_id=room.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        sess.add(s)
        emp = Employee(
            name="med-teacher",
            is_active=True,
            classroom_id=room.id,
            employee_id="MED",
        )
        sess.add(emp)
        sess.flush()
        u = _create_user(sess, employee_id=emp.id, username="med-t")

        today = date.today()
        order = StudentMedicationOrder(
            student_id=s.id,
            order_date=today,
            medication_name="退燒藥",
            dose="5ml",
            time_slots=["08:30", "13:00"],
        )
        sess.add(order)
        sess.flush()
        sess.add(
            StudentMedicationLog(
                order_id=order.id,
                scheduled_time="08:30",
                administered_at=None,
                skipped=False,
            )
        )
        sess.add(
            StudentMedicationLog(
                order_id=order.id,
                scheduled_time="13:00",
                administered_at=None,
                skipped=False,
            )
        )
        sess.commit()
        token = create_access_token(
            {"sub": u.username, "employee_id": emp.id, "permissions": -1}
        )
        resp = c.get(
            "/api/portal/class-hub/today",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["counts"]["medications_pending"] == 2
        # 08:30 → morning，13:00 → noon
        morning = next(sl for sl in body["slots"] if sl["slot_id"] == "morning")
        noon = next(sl for sl in body["slots"] if sl["slot_id"] == "noon")
        assert any(t["kind"] == "medication" for t in morning["tasks"])
        assert any(t["kind"] == "medication" for t in noon["tasks"])
        # sticky_next：取最近未過期的；以實測時間相對於 08:30/13:00 判斷
        # 不論現在幾點，至少 sticky_next 不為 None（除非已過 13:00 + N 小時）
        # 為穩定測試，僅檢查 deep_link 與 detail 格式
        if body["sticky_next"] is not None:
            assert body["sticky_next"]["kind"] == "medication"
            assert "退燒藥" in body["sticky_next"]["detail"]
            assert body["sticky_next"]["deep_link"].startswith(
                "/portal/class-hub?sheet=medication&id="
            )

    def test_no_health_perm_hides_medication(self, hub_client):
        from models.portfolio import StudentMedicationOrder, StudentMedicationLog

        c, sess = hub_client
        room = Classroom(name="P班", is_active=True)
        sess.add(room)
        sess.flush()
        s = Student(
            student_id="P1",
            name="p1",
            classroom_id=room.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        sess.add(s)
        sess.flush()
        emp = Employee(
            name="老師P", is_active=True, classroom_id=room.id, employee_id="EP"
        )
        sess.add(emp)
        sess.flush()
        # Build a User with only STUDENTS_READ — explicitly NO STUDENTS_HEALTH_READ
        from utils.permissions import Permission

        u = User(
            username="p",
            password_hash="x",
            role="teacher",
            employee_id=emp.id,
            is_active=True,
            permissions=int(Permission.STUDENTS_READ),
        )
        sess.add(u)
        sess.flush()
        # Create medication for today
        order = StudentMedicationOrder(
            student_id=s.id,
            order_date=date.today(),
            medication_name="退燒藥",
            dose="5ml",
            time_slots=["10:00"],
        )
        sess.add(order)
        sess.flush()
        sess.add(
            StudentMedicationLog(
                order_id=order.id,
                scheduled_time="10:00",
                administered_at=None,
                skipped=False,
            )
        )
        sess.commit()
        token = create_access_token(
            {
                "sub": u.username,
                "employee_id": emp.id,
                "permissions": int(Permission.STUDENTS_READ),
            }
        )
        resp = c.get(
            "/api/portal/class-hub/today",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        # medications hidden
        assert body["counts"]["medications_pending"] == 0
        all_kinds = {t["kind"] for slot in body["slots"] for t in slot["tasks"]}
        assert "medication" not in all_kinds
        assert body["sticky_next"] is None  # no sticky from medications

    def test_no_portfolio_perm_hides_observation_and_contact_book(self, hub_client):
        c, sess = hub_client
        room = Classroom(name="Q班", is_active=True)
        sess.add(room)
        sess.flush()
        s = Student(
            student_id="Q1",
            name="q1",
            classroom_id=room.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        sess.add(s)
        sess.flush()
        emp = Employee(
            name="老師Q", is_active=True, classroom_id=room.id, employee_id="EQ"
        )
        sess.add(emp)
        sess.flush()
        from utils.permissions import Permission

        # STUDENTS_READ + STUDENTS_HEALTH_READ but NO PORTFOLIO_READ
        u_perms = int(Permission.STUDENTS_READ | Permission.STUDENTS_HEALTH_READ)
        u = User(
            username="q",
            password_hash="x",
            role="teacher",
            employee_id=emp.id,
            is_active=True,
            permissions=u_perms,
        )
        sess.add(u)
        sess.commit()
        token = create_access_token(
            {
                "sub": u.username,
                "employee_id": emp.id,
                "permissions": u_perms,
            }
        )
        resp = c.get(
            "/api/portal/class-hub/today",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["counts"]["observations_pending"] == 0
        assert body["counts"]["contact_books_pending"] == 0
        all_kinds = {t["kind"] for slot in body["slots"] for t in slot["tasks"]}
        assert "observation" not in all_kinds
        assert "contact_book" not in all_kinds
        # attendance + incident still present (STUDENTS_READ granted)
        # Even if attendance count is 0 (no students yet), incident inline_button stays
        assert "incident" in all_kinds
