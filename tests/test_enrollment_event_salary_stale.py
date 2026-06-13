"""學生在籍/班籍異動 → 薪資 needs_recalc 標記（L1c，spec 2026-06-13）。

人數只影響發放月（2/6/9/12）的節慶/超額累計：事件月所屬發放月（含）之後的
發放月未封存 SalaryRecord 全標 needs_recalc（不分員工——班級人數影響全班
老師與全校比例者）。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import models.student_log  # noqa: F401 — 註冊 student_change_logs 進 Base.metadata
from models.database import Base, ClassGrade, Classroom, Employee, SalaryRecord, Student


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "enrollment-stale.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(db_engine)

    yield session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _make_employee(session, eid="E100"):
    emp = Employee(
        employee_id=eid,
        name=f"員工{eid}",
        title="幼兒園教師",
        position="幼兒園教師",
        employee_type="regular",
        base_salary=30000,
        hire_date=date(2025, 1, 1),
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_record(session, emp, year, month, *, finalized=False):
    rec = SalaryRecord(
        employee_id=emp.id,
        salary_year=year,
        salary_month=month,
        is_finalized=finalized,
        needs_recalc=False,
        version=1,
    )
    session.add(rec)
    session.flush()
    return rec


class TestMarkSalaryStaleForEnrollmentEvent:
    def test_marks_distribution_months_from_event_month(self, db):
        """3 月事件 → 6/9 月（發放月）未封存標記；2 月發放月在事件前、3 月非發放月不標。"""
        from services.salary.utils import mark_salary_stale_for_enrollment_event

        with db() as session:
            emp = _make_employee(session)
            rec_feb = _make_record(session, emp, 2026, 2)
            rec_mar = _make_record(session, emp, 2026, 3)
            rec_jun = _make_record(session, emp, 2026, 6)
            rec_sep = _make_record(session, emp, 2026, 9)

            marked = mark_salary_stale_for_enrollment_event(session, date(2026, 3, 10))
            session.flush()

            assert marked == 2
            session.refresh(rec_feb)
            session.refresh(rec_mar)
            session.refresh(rec_jun)
            session.refresh(rec_sep)
            assert rec_feb.needs_recalc is False
            assert rec_mar.needs_recalc is False
            assert rec_jun.needs_recalc is True
            assert rec_sep.needs_recalc is True

    def test_finalized_records_untouched(self, db):
        from services.salary.utils import mark_salary_stale_for_enrollment_event

        with db() as session:
            emp = _make_employee(session)
            rec = _make_record(session, emp, 2026, 6, finalized=True)
            marked = mark_salary_stale_for_enrollment_event(session, date(2026, 3, 10))
            session.flush()
            assert marked == 0
            session.refresh(rec)
            assert rec.needs_recalc is False

    def test_december_event_rolls_to_next_year_february(self, db):
        from services.salary.utils import mark_salary_stale_for_enrollment_event

        with db() as session:
            emp = _make_employee(session)
            rec_dec = _make_record(session, emp, 2025, 12)
            rec_feb = _make_record(session, emp, 2026, 2)

            marked = mark_salary_stale_for_enrollment_event(session, date(2025, 12, 5))
            session.flush()

            assert marked == 1
            session.refresh(rec_dec)
            session.refresh(rec_feb)
            # 12 月事件影響 12 月的人數 → 進「次年 2 月」發放期；
            # 2025-12 發放月結算的是 9-11 月，不受 12 月人數影響。
            assert rec_dec.needs_recalc is False
            assert rec_feb.needs_recalc is True


class TestLifecycleTransitionMarksStale:
    def test_withdraw_transition_marks_salary_stale(self, db):
        """退學（effective 3/10）→ 6 月發放月未封存薪資標 needs_recalc。"""
        from models.classroom import LIFECYCLE_WITHDRAWN
        from services.student_lifecycle import transition

        with db() as session:
            grade = ClassGrade(name="大班", is_active=True)
            session.add(grade)
            session.flush()
            room = Classroom(name="天堂鳥", grade_id=grade.id, is_active=True)
            session.add(room)
            session.flush()
            student = Student(
                student_id="S100",
                name="退學測試生",
                classroom_id=room.id,
                enrollment_date=date(2025, 9, 1),
                is_active=True,
            )
            session.add(student)
            session.flush()

            emp = _make_employee(session)
            rec = _make_record(session, emp, 2026, 6)

            transition(
                session,
                student,
                LIFECYCLE_WITHDRAWN,
                effective_date=date(2026, 3, 10),
            )
            session.flush()

            session.refresh(rec)
            assert rec.needs_recalc is True
