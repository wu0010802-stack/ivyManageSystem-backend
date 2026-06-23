"""才藝報名狀態機（第4波 階段 0）單元測試。

純新增、零行為變更：驗證 Enum 值、合法轉移表、is_*_transition_allowed 純函式、
transition_* 服務（合法更新+稽核 / 非法 enforce raise / 非法 soft warning）。
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.activity_status import (
    ActivityTransitionError,
    MatchStatus,
    OCCUPYING_COURSE_STATUSES,
    RegistrationCourseStatus,
    is_match_transition_allowed,
    is_rc_transition_allowed,
    transition_match_status,
    transition_registration_course_status,
)
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    RegistrationChange,
    RegistrationCourse,
)


class TestEnumValues:
    def test_rc_status_values_are_existing_strings(self):
        assert RegistrationCourseStatus.ENROLLED == "enrolled"
        assert RegistrationCourseStatus.WAITLIST == "waitlist"
        assert RegistrationCourseStatus.PROMOTED_PENDING == "promoted_pending"
        # str Enum：值可直接當字串用（wire 不變）
        assert RegistrationCourseStatus.ENROLLED.value == "enrolled"
        assert f"{RegistrationCourseStatus.WAITLIST.value}" == "waitlist"

    def test_match_status_values(self):
        # forced：force-accept（強行收件）寫入 DB 的既有字串值，enum 須涵蓋
        # （values 即現有 column 字串），否則狀態機 soft-enforce 後 forced row
        # 會被判為非法狀態。
        assert {s.value for s in MatchStatus} == {
            "unmatched",
            "matched",
            "pending",
            "rejected",
            "manual",
            "forced",
        }

    def test_occupying_statuses(self):
        assert OCCUPYING_COURSE_STATUSES == frozenset(
            {
                RegistrationCourseStatus.ENROLLED,
                RegistrationCourseStatus.PROMOTED_PENDING,
            }
        )


class TestRcTransitionTable:
    def test_legal_edges(self):
        assert is_rc_transition_allowed("waitlist", "promoted_pending")
        assert is_rc_transition_allowed("waitlist", "enrolled")  # manual 直升
        assert is_rc_transition_allowed("promoted_pending", "enrolled")

    def test_illegal_edges(self):
        # enrolled 終態
        assert not is_rc_transition_allowed("enrolled", "waitlist")
        assert not is_rc_transition_allowed("enrolled", "promoted_pending")
        # promoted_pending 過期是刪列、非轉回 waitlist
        assert not is_rc_transition_allowed("promoted_pending", "waitlist")

    def test_same_state_false(self):
        assert not is_rc_transition_allowed("enrolled", "enrolled")

    def test_invalid_value_raises(self):
        with pytest.raises(ActivityTransitionError):
            is_rc_transition_allowed("cancelled", "enrolled")  # cancelled 非合法值


class TestMatchTransitionTable:
    def test_legal_edges(self):
        assert is_match_transition_allowed("unmatched", "matched")
        assert is_match_transition_allowed("unmatched", "pending")
        assert is_match_transition_allowed("pending", "matched")
        assert is_match_transition_allowed("pending", "manual")
        assert is_match_transition_allowed("pending", "rejected")
        assert is_match_transition_allowed("pending", "forced")  # 強行收件
        assert is_match_transition_allowed("rejected", "pending")  # restore

    def test_illegal_edges(self):
        assert not is_match_transition_allowed("matched", "rejected")  # 終態
        assert not is_match_transition_allowed("manual", "pending")  # 終態
        assert not is_match_transition_allowed("forced", "pending")  # 終態
        assert not is_match_transition_allowed("unmatched", "rejected")
        assert not is_match_transition_allowed("unmatched", "forced")  # 須先 pending

    def test_same_state_false(self):
        assert not is_match_transition_allowed("pending", "pending")


@pytest.fixture
def sf(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sm.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _reg_with_course(session, *, rc_status="waitlist", match_status="pending"):
    course = ActivityCourse(
        name="繪畫",
        price=2000,
        capacity=10,
        school_year=115,
        semester=1,
        is_active=True,
    )
    session.add(course)
    session.flush()
    reg = ActivityRegistration(
        student_name="童",
        is_active=True,
        school_year=115,
        semester=1,
        match_status=match_status,
    )
    session.add(reg)
    session.flush()
    rc = RegistrationCourse(
        registration_id=reg.id,
        course_id=course.id,
        status=rc_status,
        price_snapshot=2000,
    )
    session.add(rc)
    session.flush()
    return reg, rc


class TestTransitionServices:
    def test_rc_legal_transition_updates_and_logs(self, sf):
        with sf() as s:
            _, rc = _reg_with_course(s, rc_status="waitlist")
            transition_registration_course_status(
                s,
                rc,
                RegistrationCourseStatus.PROMOTED_PENDING,
                operator="admin",
                enforce=True,
            )
            assert rc.status == "promoted_pending"
            logs = s.query(RegistrationChange).all()
            assert len(logs) == 1
            assert "waitlist → promoted_pending" in logs[0].description

    def test_rc_illegal_transition_enforce_raises(self, sf):
        with sf() as s:
            _, rc = _reg_with_course(s, rc_status="enrolled")
            with pytest.raises(ActivityTransitionError):
                transition_registration_course_status(
                    s,
                    rc,
                    RegistrationCourseStatus.WAITLIST,
                    operator="admin",
                    enforce=True,
                )
            assert rc.status == "enrolled"  # 未變更

    def test_rc_illegal_transition_soft_warns_and_proceeds(self, sf):
        # 階段 1 soft 模式：非法只 warning 不擋（觀察期），status 仍會被更新。
        with sf() as s:
            _, rc = _reg_with_course(s, rc_status="enrolled")
            transition_registration_course_status(
                s,
                rc,
                RegistrationCourseStatus.WAITLIST,
                operator="admin",
                enforce=False,
            )
            assert rc.status == "waitlist"  # soft：仍更新

    def test_match_legal_transition(self, sf):
        with sf() as s:
            reg, _ = _reg_with_course(s, match_status="pending")
            transition_match_status(
                s, reg, MatchStatus.REJECTED, operator="admin", enforce=True
            )
            assert reg.match_status == "rejected"

    def test_match_illegal_enforce_raises(self, sf):
        with sf() as s:
            reg, _ = _reg_with_course(s, match_status="matched")
            with pytest.raises(ActivityTransitionError):
                transition_match_status(
                    s, reg, MatchStatus.REJECTED, operator="admin", enforce=True
                )
            assert reg.match_status == "matched"
