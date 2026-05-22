"""tests/test_recruitment_funnel_transitions.py

驗 transition_visit orchestrator + visited↔deposited dispatch（Task 6 範疇）。
其他 stage dispatch 在 Task 8-10 補。
"""

import os
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Classroom, Student
from models.recruitment import RecruitmentVisit, RecruitmentEventLog
import models.student_log  # noqa: F401 — ensures student_change_logs table is registered in metadata
from services.recruitment_funnel import (
    transition_visit,
    RecruitmentFunnelError,
)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _make_visit(session, *, has_deposit=False, enrolled=False) -> RecruitmentVisit:
    v = RecruitmentVisit(
        month="115.03",
        child_name="測試幼生",
        has_deposit=has_deposit,
        enrolled=enrolled,
    )
    session.add(v)
    session.flush()
    return v


class TestVisitedDeposited:
    def test_visited_to_deposited(self, session):
        visit = _make_visit(session, has_deposit=False)
        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="deposited",
            actor_user_id=99,
        )
        session.flush()

        assert result.from_stage == "visited"
        assert result.to_stage == "deposited"
        assert result.student_id is None
        assert result.event_log_id > 0

        session.refresh(visit)
        assert visit.has_deposit is True

        log = (
            session.query(RecruitmentEventLog)
            .filter_by(recruitment_visit_id=visit.id)
            .one()
        )
        assert log.event_type == "deposit_added"
        assert log.actor_user_id == 99

    def test_deposited_to_visited(self, session):
        visit = _make_visit(session, has_deposit=True)
        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="visited",
            actor_user_id=99,
        )
        session.flush()
        session.refresh(visit)
        assert visit.has_deposit is False
        log = (
            session.query(RecruitmentEventLog)
            .filter_by(recruitment_visit_id=visit.id)
            .one()
        )
        assert log.event_type == "deposit_removed"

    def test_same_stage_returns_409_like_error(self, session):
        visit = _make_visit(session, has_deposit=False)
        with pytest.raises(RecruitmentFunnelError) as exc:
            transition_visit(
                session,
                visit_id=visit.id,
                to_stage="visited",
                actor_user_id=99,
            )
        assert exc.value.code == "STAGE_ALREADY"

    def test_visit_not_found(self, session):
        with pytest.raises(RecruitmentFunnelError) as exc:
            transition_visit(
                session,
                visit_id=999999,
                to_stage="deposited",
                actor_user_id=99,
            )
        assert exc.value.code == "VISIT_NOT_FOUND"

    def test_returns_warnings_empty_list(self, session):
        visit = _make_visit(session, has_deposit=False)
        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="deposited",
            actor_user_id=99,
        )
        assert result.warnings == []


@pytest.fixture
def classroom(session):
    c = Classroom(name="小班-甲", school_year=114, semester=1, class_code="A")
    session.add(c)
    session.flush()
    return c


class TestDepositedToEnrolled:
    def test_forward_creates_student(self, session, classroom):
        visit = _make_visit(session, has_deposit=True)
        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="enrolled",
            actor_user_id=99,
            classroom_id=classroom.id,
        )
        session.flush()
        assert result.student_id is not None
        student = session.get(Student, result.student_id)
        assert student.lifecycle_status == "enrolled"
        assert student.recruitment_visit_id == visit.id

        log = (
            session.query(RecruitmentEventLog)
            .filter_by(recruitment_visit_id=visit.id, event_type="converted")
            .one()
        )
        assert log.student_id == student.id

    def test_missing_classroom_raises(self, session):
        visit = _make_visit(session, has_deposit=True)
        with pytest.raises(RecruitmentFunnelError) as exc:
            transition_visit(
                session,
                visit_id=visit.id,
                to_stage="enrolled",
                actor_user_id=99,
                classroom_id=None,
            )
        assert exc.value.code == "CONVERT_NEED_CLASSROOM"


class TestEnrolledActive:
    def test_enrolled_to_active(self, session, classroom):
        visit = _make_visit(session, has_deposit=True, enrolled=True)
        student = Student(
            student_id="115-A-01",
            name="測試生",
            lifecycle_status="enrolled",
            recruitment_visit_id=visit.id,
            is_active=True,
        )
        session.add(student)
        session.flush()
        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="active",
            actor_user_id=99,
        )
        session.flush()
        session.refresh(student)
        assert student.lifecycle_status == "active"
        log = (
            session.query(RecruitmentEventLog)
            .filter_by(recruitment_visit_id=visit.id, event_type="activated")
            .one()
        )
        assert log.actor_user_id == 99

    def test_active_to_enrolled_with_reason(self, session):
        visit = _make_visit(session, has_deposit=True, enrolled=True)
        student = Student(
            student_id="115-A-02",
            name="測試生2",
            lifecycle_status="active",
            recruitment_visit_id=visit.id,
            is_active=True,
        )
        session.add(student)
        session.flush()
        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="enrolled",
            actor_user_id=99,
            reason="校方臨時暫緩開學",
        )
        session.flush()
        session.refresh(student)
        assert student.lifecycle_status == "enrolled"
        log = (
            session.query(RecruitmentEventLog)
            .filter_by(recruitment_visit_id=visit.id, event_type="revert_activated")
            .one()
        )
        assert log.reason == "校方臨時暫緩開學"

    def test_active_to_enrolled_with_attendance_warns(self, session):
        from datetime import date

        from models.classroom import StudentAttendance

        visit = _make_visit(session, has_deposit=True, enrolled=True)
        student = Student(
            student_id="115-A-03",
            name="測試生3",
            lifecycle_status="active",
            recruitment_visit_id=visit.id,
            is_active=True,
        )
        session.add(student)
        session.flush()
        att = StudentAttendance(student_id=student.id, date=date(2026, 5, 1))
        session.add(att)
        session.flush()
        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="enrolled",
            actor_user_id=99,
            reason="原因",
        )
        assert "student_has_attendance_after_active" in result.warnings
