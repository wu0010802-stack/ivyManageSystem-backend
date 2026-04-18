"""tests/test_student_lifecycle.py — 學生生命週期狀態機測試

Red/Green/Refactor：純邏輯先行（不寫 API）。
狀態機的合法轉移表定義於 services/student_lifecycle.py::ALLOWED_TRANSITIONS。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ENROLLED,
    LIFECYCLE_GRADUATED,
    LIFECYCLE_ON_LEAVE,
    LIFECYCLE_PROSPECT,
    LIFECYCLE_STATUSES,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
    Classroom,
    Student,
)
from models.student_log import StudentChangeLog
from services.student_lifecycle import (
    ALLOWED_TRANSITIONS,
    LifecycleTransitionError,
    get_event_type_for_transition,
    is_transition_allowed,
    transition,
)


# ============ Fixtures ============


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


@pytest.fixture
def classroom(session):
    c = Classroom(name="測試班", school_year=114, semester=1)
    session.add(c)
    session.flush()
    return c


def _make_student(session, classroom, *, lifecycle_status=LIFECYCLE_ACTIVE):
    s = Student(
        student_id=f"T{len(session.query(Student).all()) + 1:03d}",
        name="測試生",
        classroom_id=classroom.id,
        lifecycle_status=lifecycle_status,
        is_active=lifecycle_status
        not in (LIFECYCLE_WITHDRAWN, LIFECYCLE_TRANSFERRED, LIFECYCLE_GRADUATED),
        enrollment_date=date(2026, 2, 1),
    )
    session.add(s)
    session.flush()
    return s


# ============ 純函式：is_transition_allowed ============


class TestIsTransitionAllowed:
    def test_active_to_on_leave_allowed(self):
        assert is_transition_allowed(LIFECYCLE_ACTIVE, LIFECYCLE_ON_LEAVE) is True

    def test_active_to_graduated_allowed(self):
        assert is_transition_allowed(LIFECYCLE_ACTIVE, LIFECYCLE_GRADUATED) is True

    def test_active_to_prospect_rejected(self):
        # 學生不能倒退回 prospect
        assert is_transition_allowed(LIFECYCLE_ACTIVE, LIFECYCLE_PROSPECT) is False

    def test_graduated_is_terminal(self):
        for target in LIFECYCLE_STATUSES:
            assert (
                is_transition_allowed(LIFECYCLE_GRADUATED, target) is False
            ), f"graduated → {target} 應被拒絕"

    def test_transferred_is_terminal(self):
        for target in LIFECYCLE_STATUSES:
            assert is_transition_allowed(LIFECYCLE_TRANSFERRED, target) is False

    def test_withdrawn_can_return_active(self):
        # 退學後可復學
        assert is_transition_allowed(LIFECYCLE_WITHDRAWN, LIFECYCLE_ACTIVE) is True

    def test_withdrawn_cannot_goto_graduated(self):
        assert is_transition_allowed(LIFECYCLE_WITHDRAWN, LIFECYCLE_GRADUATED) is False

    def test_on_leave_to_active(self):
        assert is_transition_allowed(LIFECYCLE_ON_LEAVE, LIFECYCLE_ACTIVE) is True

    def test_same_status_is_rejected(self):
        # 相同狀態不算轉移（避免多餘 ChangeLog）
        assert is_transition_allowed(LIFECYCLE_ACTIVE, LIFECYCLE_ACTIVE) is False

    def test_invalid_status_raises(self):
        with pytest.raises(LifecycleTransitionError):
            is_transition_allowed("nonsense", LIFECYCLE_ACTIVE)
        with pytest.raises(LifecycleTransitionError):
            is_transition_allowed(LIFECYCLE_ACTIVE, "nonsense")


class TestGetEventType:
    def test_active_to_on_leave_returns_休學(self):
        assert (
            get_event_type_for_transition(LIFECYCLE_ACTIVE, LIFECYCLE_ON_LEAVE)
            == "休學"
        )

    def test_active_to_graduated_returns_畢業(self):
        assert (
            get_event_type_for_transition(LIFECYCLE_ACTIVE, LIFECYCLE_GRADUATED)
            == "畢業"
        )

    def test_on_leave_to_active_returns_復學(self):
        assert (
            get_event_type_for_transition(LIFECYCLE_ON_LEAVE, LIFECYCLE_ACTIVE)
            == "復學"
        )

    def test_active_to_transferred_returns_轉出(self):
        assert (
            get_event_type_for_transition(LIFECYCLE_ACTIVE, LIFECYCLE_TRANSFERRED)
            == "轉出"
        )

    def test_active_to_withdrawn_returns_退學(self):
        assert (
            get_event_type_for_transition(LIFECYCLE_ACTIVE, LIFECYCLE_WITHDRAWN)
            == "退學"
        )

    def test_illegal_transition_raises(self):
        with pytest.raises(LifecycleTransitionError):
            get_event_type_for_transition(LIFECYCLE_GRADUATED, LIFECYCLE_ACTIVE)


# ============ 整合：transition() 會寫 ChangeLog 並更新 Student ============


class TestTransitionIntegration:
    def test_active_to_on_leave_writes_changelog(self, session, classroom):
        student = _make_student(session, classroom)
        result = transition(
            session,
            student,
            to_status=LIFECYCLE_ON_LEAVE,
            effective_date=date(2026, 3, 1),
            reason="家庭因素",
        )
        session.commit()

        assert student.lifecycle_status == LIFECYCLE_ON_LEAVE
        assert student.is_active is True  # 休學仍算在讀
        logs = session.query(StudentChangeLog).filter_by(student_id=student.id).all()
        assert len(logs) == 1
        assert logs[0].event_type == "休學"
        assert logs[0].event_date == date(2026, 3, 1)
        assert logs[0].reason == "家庭因素"
        assert result.event_type == "休學"

    def test_active_to_graduated_sets_flags(self, session, classroom):
        student = _make_student(session, classroom)
        transition(
            session,
            student,
            to_status=LIFECYCLE_GRADUATED,
            effective_date=date(2026, 7, 31),
        )
        session.commit()

        assert student.lifecycle_status == LIFECYCLE_GRADUATED
        assert student.is_active is False
        assert student.status == "已畢業"
        assert student.graduation_date == date(2026, 7, 31)

    def test_active_to_transferred_sets_withdrawal_date(self, session, classroom):
        student = _make_student(session, classroom)
        transition(
            session,
            student,
            to_status=LIFECYCLE_TRANSFERRED,
            effective_date=date(2026, 5, 1),
        )
        session.commit()

        assert student.lifecycle_status == LIFECYCLE_TRANSFERRED
        assert student.is_active is False
        assert student.status == "已轉出"
        assert student.withdrawal_date == date(2026, 5, 1)

    def test_withdrawn_can_return_active(self, session, classroom):
        student = _make_student(session, classroom, lifecycle_status=LIFECYCLE_WITHDRAWN)
        student.is_active = False
        student.status = "已退學"
        student.withdrawal_date = date(2026, 3, 1)
        session.flush()

        transition(
            session,
            student,
            to_status=LIFECYCLE_ACTIVE,
            effective_date=date(2026, 5, 1),
            reason="復學",
        )
        session.commit()

        assert student.lifecycle_status == LIFECYCLE_ACTIVE
        assert student.is_active is True
        assert student.withdrawal_date is None
        # 檢查 ChangeLog 寫的是「復學」
        logs = session.query(StudentChangeLog).filter_by(student_id=student.id).all()
        assert logs[-1].event_type == "復學"

    def test_illegal_transition_raises_and_no_changelog(self, session, classroom):
        student = _make_student(session, classroom, lifecycle_status=LIFECYCLE_GRADUATED)
        student.is_active = False
        session.flush()

        with pytest.raises(LifecycleTransitionError):
            transition(session, student, to_status=LIFECYCLE_ACTIVE)

        # 不應寫出任何 log
        logs = session.query(StudentChangeLog).filter_by(student_id=student.id).all()
        assert len(logs) == 0

    def test_same_status_is_rejected(self, session, classroom):
        student = _make_student(session, classroom)
        with pytest.raises(LifecycleTransitionError):
            transition(session, student, to_status=LIFECYCLE_ACTIVE)

    def test_transferred_is_terminal_cannot_reactivate(self, session, classroom):
        student = _make_student(
            session, classroom, lifecycle_status=LIFECYCLE_TRANSFERRED
        )
        student.is_active = False
        session.flush()

        with pytest.raises(LifecycleTransitionError):
            transition(session, student, to_status=LIFECYCLE_ACTIVE)


# ============ 資料完整性：ALLOWED_TRANSITIONS 所有 event_key 皆可查表 ============


class TestTransitionTableIntegrity:
    def test_all_event_keys_have_mapping(self):
        from models.student_log import LIFECYCLE_TO_EVENT_TYPE

        for from_status, targets in ALLOWED_TRANSITIONS.items():
            for to_status, event_key in targets.items():
                assert event_key in LIFECYCLE_TO_EVENT_TYPE, (
                    f"{from_status} → {to_status} 的 event_key "
                    f"{event_key!r} 未在 LIFECYCLE_TO_EVENT_TYPE 定義"
                )

    def test_all_statuses_have_entry_in_transitions(self):
        # 每個 status 都要在 ALLOWED_TRANSITIONS 有 key（即使是空 dict = 終態）
        for status in LIFECYCLE_STATUSES:
            assert status in ALLOWED_TRANSITIONS
