"""教師首頁彙總（/api/portal/home/summary）+ portal_dashboard_service 測試。"""

from __future__ import annotations

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
from api.portal import router as portal_router
from models.classroom import LIFECYCLE_ACTIVE
from models.database import (
    Base,
    Classroom,
    Employee,
    Student,
    StudentAttendance,
    User,
)
from models.portfolio import (
    StudentAllergy,
    StudentMedicationLog,
    StudentMedicationOrder,
)
from services.portal_dashboard_service import (
    compute_allergy_alerts,
    compute_consecutive_absences,
    compute_upcoming_birthdays,
    count_pending_medications,
    has_attendance_today,
)
from utils.auth import create_access_token
from utils.permissions import Permission

# ════════════════════════════════════════════════════════════════════════
# 純函式單元測試（直接造 DB session）
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture
def in_mem_session(tmp_path):
    db_path = tmp_path / "dash.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    sess = sf()
    yield sess
    sess.close()
    engine.dispose()


def _seed_classroom_with_students(sess, *, count: int = 2) -> Classroom:
    c = Classroom(name="A班", is_active=True)
    sess.add(c)
    sess.flush()
    for i in range(count):
        s = Student(
            student_id=f"S{i+1}",
            name=f"小學{i+1}",
            classroom_id=c.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        sess.add(s)
    sess.flush()
    return c


class TestComputeConsecutiveAbsences:
    def test_three_day_streak_above_threshold(self, in_mem_session):
        sess = in_mem_session
        c = _seed_classroom_with_students(sess, count=1)
        student = sess.query(Student).first()
        today = date(2026, 5, 5)
        # 連續 5/4 5/3 5/2 三天缺席
        for d in (date(2026, 5, 4), date(2026, 5, 3), date(2026, 5, 2)):
            sess.add(StudentAttendance(student_id=student.id, date=d, status="缺席"))
        sess.commit()
        result = compute_consecutive_absences(
            sess, classroom_id=c.id, today=today, threshold_days=2
        )
        assert len(result) == 1
        assert result[0]["student_id"] == student.id
        assert result[0]["days"] == 3

    def test_streak_broken_by_present(self, in_mem_session):
        sess = in_mem_session
        c = _seed_classroom_with_students(sess, count=1)
        student = sess.query(Student).first()
        today = date(2026, 5, 5)
        sess.add(
            StudentAttendance(
                student_id=student.id, date=date(2026, 5, 4), status="缺席"
            )
        )
        sess.add(
            StudentAttendance(
                student_id=student.id, date=date(2026, 5, 3), status="出席"
            )
        )
        sess.add(
            StudentAttendance(
                student_id=student.id, date=date(2026, 5, 2), status="缺席"
            )
        )
        sess.commit()
        result = compute_consecutive_absences(
            sess, classroom_id=c.id, today=today, threshold_days=2
        )
        # 5/4 後 5/3 出席就斷了，連續=1，未達 threshold 2
        assert result == []

    def test_leave_does_not_count_as_absent(self, in_mem_session):
        """請假類別（病假/事假）不算連續缺席。"""
        sess = in_mem_session
        c = _seed_classroom_with_students(sess, count=1)
        student = sess.query(Student).first()
        today = date(2026, 5, 5)
        for d in (date(2026, 5, 4), date(2026, 5, 3)):
            sess.add(StudentAttendance(student_id=student.id, date=d, status="病假"))
        sess.commit()
        result = compute_consecutive_absences(
            sess, classroom_id=c.id, today=today, threshold_days=2
        )
        assert result == []


class TestComputeUpcomingBirthdays:
    def test_within_window(self, in_mem_session):
        sess = in_mem_session
        c = _seed_classroom_with_students(sess, count=2)
        students = sess.query(Student).order_by(Student.id).all()
        today = date(2026, 5, 1)
        students[0].birthday = date(2020, 5, 5)  # 4 天後
        students[1].birthday = date(2020, 6, 1)  # 31 天後（超過 7d）
        sess.commit()

        result = compute_upcoming_birthdays(
            sess, classroom_id=c.id, today=today, window_days=7
        )
        assert len(result) == 1
        assert result[0]["student_id"] == students[0].id
        assert result[0]["days_until"] == 4
        assert result[0]["age_turning"] == 6

    def test_today_birthday_included(self, in_mem_session):
        sess = in_mem_session
        c = _seed_classroom_with_students(sess, count=1)
        s = sess.query(Student).first()
        today = date(2026, 5, 5)
        s.birthday = date(2020, 5, 5)
        sess.commit()
        result = compute_upcoming_birthdays(
            sess, classroom_id=c.id, today=today, window_days=7
        )
        assert len(result) == 1
        assert result[0]["days_until"] == 0


class TestComputeAllergyAlerts:
    def test_only_active_listed(self, in_mem_session):
        sess = in_mem_session
        c = _seed_classroom_with_students(sess, count=1)
        s = sess.query(Student).first()
        sess.add(
            StudentAllergy(
                student_id=s.id, allergen="花生", severity="severe", active=True
            )
        )
        sess.add(
            StudentAllergy(
                student_id=s.id, allergen="塵蟎", severity="mild", active=False
            )
        )
        sess.commit()
        result = compute_allergy_alerts(sess, classroom_id=c.id)
        assert len(result) == 1
        allergens = [a["allergen"] for a in result[0]["allergens"]]
        assert allergens == ["花生"]


class TestCountPendingMedications:
    def test_only_today_pending(self, in_mem_session):
        sess = in_mem_session
        c = _seed_classroom_with_students(sess, count=1)
        s = sess.query(Student).first()
        today = date(2026, 5, 5)
        order = StudentMedicationOrder(
            student_id=s.id,
            order_date=today,
            medication_name="退燒藥",
            dose="5ml",
            time_slots=["08:30", "12:00"],
            source="parent",
        )
        sess.add(order)
        sess.flush()
        # 預建 logs
        sess.add(StudentMedicationLog(order_id=order.id, scheduled_time="08:30"))
        sess.add(StudentMedicationLog(order_id=order.id, scheduled_time="12:00"))
        sess.commit()
        n = count_pending_medications(sess, classroom_id=c.id, today=today)
        assert n == 2

    def test_administered_not_counted(self, in_mem_session):
        from datetime import datetime as _dt

        sess = in_mem_session
        c = _seed_classroom_with_students(sess, count=1)
        s = sess.query(Student).first()
        today = date(2026, 5, 5)
        order = StudentMedicationOrder(
            student_id=s.id,
            order_date=today,
            medication_name="x",
            dose="1",
            time_slots=["08:30"],
            source="parent",
        )
        sess.add(order)
        sess.flush()
        sess.add(
            StudentMedicationLog(
                order_id=order.id,
                scheduled_time="08:30",
                administered_at=_dt.now(),
                administered_by=1,
            )
        )
        sess.commit()
        assert count_pending_medications(sess, classroom_id=c.id, today=today) == 0


class TestHasAttendanceToday:
    def test_no_records_returns_false(self, in_mem_session):
        sess = in_mem_session
        c = _seed_classroom_with_students(sess, count=2)
        assert (
            has_attendance_today(sess, classroom_id=c.id, today=date(2026, 5, 5))
            is False
        )

    def test_partial_records_returns_true(self, in_mem_session):
        sess = in_mem_session
        c = _seed_classroom_with_students(sess, count=2)
        students = sess.query(Student).all()
        sess.add(
            StudentAttendance(
                student_id=students[0].id, date=date(2026, 5, 5), status="出席"
            )
        )
        sess.commit()
        assert (
            has_attendance_today(sess, classroom_id=c.id, today=date(2026, 5, 5))
            is True
        )


# ════════════════════════════════════════════════════════════════════════
# 整合：/api/portal/home/summary endpoint
# ════════════════════════════════════════════════════════════════════════


@pytest.fixture
def home_client(tmp_path):
    db_path = tmp_path / "home.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(portal_router)
    with TestClient(app) as c:
        yield c, sf

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_teacher(sf) -> dict:
    """造一位班導 + 3 個學生班，回傳 primitive 值（避免 DetachedInstance）。"""
    perm = int(
        Permission.DASHBOARD.value
        | Permission.PORTFOLIO_READ.value
        | Permission.PORTFOLIO_WRITE.value
        | Permission.PARENT_MESSAGES_WRITE.value
    )
    with sf() as session:
        emp = Employee(
            employee_id="E01", name="王老師", is_active=True, base_salary=30000
        )
        session.add(emp)
        session.flush()
        classroom = Classroom(name="向日葵", is_active=True, head_teacher_id=emp.id)
        session.add(classroom)
        session.flush()
        for i in range(3):
            session.add(
                Student(
                    student_id=f"S{i}",
                    name=f"小朋友{i}",
                    classroom_id=classroom.id,
                    is_active=True,
                    lifecycle_status=LIFECYCLE_ACTIVE,
                )
            )
        u = User(
            username="t1",
            password_hash="!",
            role="teacher",
            employee_id=emp.id,
            permissions=perm,
            is_active=True,
            token_version=0,
        )
        session.add(u)
        session.commit()
        return {
            "user_id": u.id,
            "username": u.username,
            "token_version": u.token_version or 0,
            "employee_id": emp.id,
            "classroom_id": classroom.id,
            "permissions": perm,
        }


def _token(seed: dict) -> str:
    return create_access_token(
        {
            "user_id": seed["user_id"],
            "employee_id": seed["employee_id"],
            "role": "teacher",
            "name": seed["username"],
            "permissions": seed["permissions"],
            "token_version": seed["token_version"],
        }
    )


class TestHomeSummary:
    def test_basic_summary_returns_classroom_card(self, home_client):
        client, sf = home_client
        seed = _seed_teacher(sf)
        tk = _token(seed)
        rsp = client.get("/api/portal/home/summary", cookies={"access_token": tk})
        assert rsp.status_code == 200, rsp.text
        body = rsp.json()
        assert body["me"]["employee_id"] == seed["employee_id"]
        assert body["me"]["name"] == "王老師"
        assert "today" in body
        assert len(body["classrooms"]) == 1
        card = body["classrooms"][0]
        assert card["classroom_name"] == "向日葵"
        assert card["student_count"] == 3
        assert card["contact_book"]["roster"] == 3
        assert card["contact_book"]["published"] == 0
        assert card["pending_dismissal_calls"] == 0
        assert card["pending_medications_today"] == 0
        # actions 全部 0
        assert body["actions"]["unread_messages"] == 0
        assert body["actions"]["pending_substitute"] == 0
        assert body["actions"]["pending_swap"] == 0

    def test_summary_includes_birthday_in_window(self, home_client):
        client, sf = home_client
        seed = _seed_teacher(sf)
        with sf() as session:
            s = (
                session.query(Student)
                .filter(Student.classroom_id == seed["classroom_id"])
                .first()
            )
            today = date.today()
            # 設成 3 天後生日
            s.birthday = (today + timedelta(days=3)).replace(year=2020)
            session.commit()
        tk = _token(seed)
        rsp = client.get("/api/portal/home/summary", cookies={"access_token": tk})
        assert rsp.status_code == 200
        card = rsp.json()["classrooms"][0]
        assert len(card["upcoming_birthdays_7d"]) == 1
        assert card["upcoming_birthdays_7d"][0]["days_until"] == 3
