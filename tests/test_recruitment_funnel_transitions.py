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
import models.fees  # noqa: F401 — ensures student_fee_records table is registered in metadata
import models.portfolio  # noqa: F401 — ensures portfolio tables are registered in metadata
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

    def test_enrolled_to_active_writes_lifecycle_audit(self, session, classroom):
        """activate 經 set_lifecycle_status 統一入口寫全站 AuditLog（修補繞過缺口）。"""
        from models.audit import AuditLog

        visit = _make_visit(session, has_deposit=True, enrolled=True)
        student = Student(
            student_id="115-A-09",
            name="稽核生",
            lifecycle_status="enrolled",
            recruitment_visit_id=visit.id,
            is_active=True,
        )
        session.add(student)
        session.flush()
        transition_visit(
            session, visit_id=visit.id, to_stage="active", actor_user_id=99
        )
        session.flush()
        audit = (
            session.query(AuditLog)
            .filter_by(entity_type="student", entity_id=str(student.id))
            .one()
        )
        assert "active" in audit.summary
        assert audit.user_id == 99

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


class TestDestructiveReverts:
    def test_enrolled_to_deposited_clean(self, session):
        """無下游資料 → 刪 Student，flip visit.enrolled=False，寫 revert_converted log。"""
        from models.guardian import Guardian

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
        guardian = Guardian(
            student_id=student.id,
            name="家長",
            phone="0900000000",
            relation="父",
            is_primary=True,
            can_pickup=True,
            sort_order=0,
        )
        session.add(guardian)
        session.flush()
        student_id_before = student.id

        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="deposited",
            actor_user_id=99,
            reason="家長取消報到",
        )
        session.flush()
        # Student 應被刪
        assert session.get(Student, student_id_before) is None
        # visit.enrolled flip
        session.refresh(visit)
        assert visit.enrolled is False
        assert visit.has_deposit is True  # 退到 deposited
        # event log
        log = (
            session.query(RecruitmentEventLog)
            .filter_by(
                recruitment_visit_id=visit.id,
                event_type="revert_converted",
            )
            .one()
        )
        assert log.reason == "家長取消報到"
        # student_id 因 SET NULL FK 應為 None；metadata 內保留 deleted_student_id
        # SQLite 上 SET NULL 行為依 PRAGMA — 此處只檢查 metadata
        assert (log.metadata_json or {}).get("deleted_student_id") == student_id_before

    def test_enrolled_to_deposited_with_attendance_blocks(self, session):
        from datetime import date
        from models.classroom import StudentAttendance

        visit = _make_visit(session, has_deposit=True, enrolled=True)
        student = Student(
            student_id="115-A-02",
            name="測試生2",
            lifecycle_status="enrolled",
            recruitment_visit_id=visit.id,
            is_active=True,
        )
        session.add(student)
        session.flush()
        att = StudentAttendance(student_id=student.id, date=date(2026, 5, 1))
        session.add(att)
        session.flush()
        with pytest.raises(RecruitmentFunnelError) as exc:
            transition_visit(
                session,
                visit_id=visit.id,
                to_stage="deposited",
                actor_user_id=99,
                reason="家長取消報到",
            )
        assert exc.value.code == "REVERT_STUDENT_HAS_DATA"
        assert session.get(Student, student.id) is not None  # 沒被刪

    def test_destructive_without_reason_raises(self, session):
        visit = _make_visit(session, has_deposit=True, enrolled=True)
        Student_inst = Student(
            student_id="115-A-03",
            name="測試生3",
            lifecycle_status="enrolled",
            recruitment_visit_id=visit.id,
            is_active=True,
        )
        session.add(Student_inst)
        session.flush()
        with pytest.raises(RecruitmentFunnelError) as exc:
            transition_visit(
                session,
                visit_id=visit.id,
                to_stage="deposited",
                actor_user_id=99,
                reason="",
            )
        assert exc.value.code == "REASON_REQUIRED"

    def test_enrolled_to_visited_chains(self, session):
        """enrolled → visited 應走兩段：先 revert_converted 再 deposit_removed。"""
        visit = _make_visit(session, has_deposit=True, enrolled=True)
        student = Student(
            student_id="115-A-04",
            name="測試生4",
            lifecycle_status="enrolled",
            recruitment_visit_id=visit.id,
            is_active=True,
        )
        session.add(student)
        session.flush()
        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="visited",
            actor_user_id=99,
            reason="家長改變心意",
        )
        session.flush()
        session.refresh(visit)
        assert visit.has_deposit is False
        assert visit.enrolled is False
        # 應有 2 筆 event log（revert_converted + deposit_removed）
        logs = (
            session.query(RecruitmentEventLog)
            .filter_by(
                recruitment_visit_id=visit.id,
            )
            .order_by(RecruitmentEventLog.id)
            .all()
        )
        types = [l.event_type for l in logs]
        assert "revert_converted" in types
        assert "deposit_removed" in types

    def test_active_to_visited_chains(self, session):
        """active → visited 應走三段：revert_activated → revert_converted → deposit_removed。"""
        visit = _make_visit(session, has_deposit=True, enrolled=True)
        student = Student(
            student_id="115-A-05",
            name="測試生5",
            lifecycle_status="active",
            recruitment_visit_id=visit.id,
            is_active=True,
        )
        session.add(student)
        session.flush()
        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="visited",
            actor_user_id=99,
            reason="退學",
        )
        session.flush()
        session.refresh(visit)
        assert visit.has_deposit is False
        assert visit.enrolled is False
        logs = (
            session.query(RecruitmentEventLog)
            .filter_by(
                recruitment_visit_id=visit.id,
            )
            .order_by(RecruitmentEventLog.id)
            .all()
        )
        types = [l.event_type for l in logs]
        assert "revert_activated" in types
        assert "revert_converted" in types
        assert "deposit_removed" in types


# ── R4-7：非法跨段轉換拋 RecruitmentFunnelError（caller→400），非 500 ──


def test_illegal_cross_stage_raises_funnel_error_not_internimplemented(session):
    """visited→enrolled（跳過 deposited）等非法跨段轉換須拋 RecruitmentFunnelError
    （caller catch → 400），而非未捕捉的 NotImplementedError（→ 500）。"""
    visit = _make_visit(session, has_deposit=False)
    with pytest.raises(RecruitmentFunnelError) as exc:
        transition_visit(
            session,
            visit_id=visit.id,
            to_stage="enrolled",
            actor_user_id=1,
        )
    assert exc.value.code == "ILLEGAL_TRANSITION"
