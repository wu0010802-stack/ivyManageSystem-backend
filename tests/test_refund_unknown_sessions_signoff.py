"""tests/test_refund_unknown_sessions_signoff.py — sessions IS NULL 強制簽核守衛。

業主裁示（P3 修補，2026-06-22）：
  課程 sessions IS NULL（admin 未設定總堂數）時，server-side 無法算出正確建議退費，
  此類退費一律需要 ACTIVITY_PAYMENT_APPROVE 簽核，不論 actual==suggested（diff=0）。

涵蓋：
  (a) course.sessions=NULL → build_refund_suggestion 回傳 needs_manual_review=True
  (b) needs_manual_review=True，無 ACTIVITY_PAYMENT_APPROVE → 403（即使 diff=0）
  (c) 防迴歸：sessions 已知，diff=0 → 不要求簽核；needs_manual_review=True 有 APPROVE → 放行
"""

import os
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    RegistrationCourse,
)
from services.activity_payment_guards import require_approve_for_refund_diff
from services.activity_refund_query import build_refund_suggestion

# ── fixtures & helpers ──────────────────────────────────────────────────────

APPROVER = {"permission_names": ["ACTIVITY_PAYMENT_APPROVE"]}
LINE_STAFF = {"permission_names": []}


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "refund_unknown_sessions.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _create_course(session, *, name="圍棋", sessions, price=1500):
    c = ActivityCourse(
        name=name,
        price=price,
        sessions=sessions,
        capacity=30,
        school_year=114,
        semester=1,
    )
    session.add(c)
    session.flush()
    return c


def _create_reg(session, *, student_name="王小明"):
    reg = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name="大班",
        school_year=114,
        semester=1,
        paid_amount=1500,
        is_paid=True,
        is_active=True,
    )
    session.add(reg)
    session.flush()
    return reg


def _enroll_course(session, reg_id, course_id, price=1500):
    rc = RegistrationCourse(
        registration_id=reg_id,
        course_id=course_id,
        status="enrolled",
        price_snapshot=price,
    )
    session.add(rc)
    session.flush()


# ── (a) build_refund_suggestion 回傳 needs_manual_review ────────────────────


class TestBuildRefundSuggestionNeedsManualReview:
    def test_sessions_null_sets_needs_manual_review_true(self, db_session):
        """course.sessions=NULL → build_refund_suggestion 回傳 needs_manual_review=True。"""
        course = _create_course(db_session, sessions=None, price=1500)
        reg = _create_reg(db_session)
        _enroll_course(db_session, reg.id, course.id, price=1500)

        result = build_refund_suggestion(db_session, reg.id)

        assert result["needs_manual_review"] is True

    def test_sessions_zero_sets_needs_manual_review_true(self, db_session):
        """course.sessions=0（≤0）→ needs_manual_review=True。"""
        course = _create_course(db_session, sessions=0, price=1500)
        reg = _create_reg(db_session)
        _enroll_course(db_session, reg.id, course.id, price=1500)

        result = build_refund_suggestion(db_session, reg.id)

        assert result["needs_manual_review"] is True

    def test_sessions_known_needs_manual_review_false(self, db_session):
        """course.sessions=10（已知）→ needs_manual_review=False。"""
        course = _create_course(db_session, sessions=10, price=1500)
        reg = _create_reg(db_session)
        _enroll_course(db_session, reg.id, course.id, price=1500)

        result = build_refund_suggestion(db_session, reg.id)

        assert result["needs_manual_review"] is False

    def test_mixed_courses_one_null_sets_true(self, db_session):
        """reg 有 2 課程：1 堂數已知 + 1 NULL → needs_manual_review=True（任一未知即標記）。"""
        course_known = _create_course(
            db_session, name="已知課", sessions=10, price=1000
        )
        course_null = _create_course(
            db_session, name="NULL課", sessions=None, price=500
        )
        reg = _create_reg(db_session)
        _enroll_course(db_session, reg.id, course_known.id, price=1000)
        _enroll_course(db_session, reg.id, course_null.id, price=500)

        result = build_refund_suggestion(db_session, reg.id)

        assert result["needs_manual_review"] is True


# ── (b) require_approve_for_refund_diff 守衛 ────────────────────────────────


class TestRequireApproveForRefundDiffNeedsManualReview:
    def test_needs_manual_review_true_no_approve_raises_403_even_if_diff_zero(self):
        """needs_manual_review=True + 無 APPROVE + diff=0 → 仍 403。

        這是本次修補的核心：全退剛好等於 suggested，diff=0，原本不觸發守衛，
        但因 sessions 未知，依業主裁示仍需強制簽核。
        """
        suggestion = {
            "total_suggested_amount": 1500,
            "needs_manual_review": True,
        }
        with pytest.raises(HTTPException) as exc:
            require_approve_for_refund_diff(
                diff=0,
                current_user=LINE_STAFF,
                suggested_total=1500,
                actual_total=1500,
                suggestion=suggestion,
            )
        assert exc.value.status_code == 403

    def test_needs_manual_review_true_no_approve_raises_403_with_diff_nonzero(self):
        """needs_manual_review=True + 無 APPROVE + diff=200 → 403（原本也會 403，加旗標不影響）。"""
        suggestion = {
            "total_suggested_amount": 1500,
            "needs_manual_review": True,
        }
        with pytest.raises(HTTPException) as exc:
            require_approve_for_refund_diff(
                diff=200,
                current_user=LINE_STAFF,
                suggested_total=1500,
                actual_total=1300,
                suggestion=suggestion,
            )
        assert exc.value.status_code == 403

    def test_needs_manual_review_true_with_approve_passes(self):
        """needs_manual_review=True + 有 APPROVE → 放行（即使 diff=0 也可過）。"""
        suggestion = {
            "total_suggested_amount": 1500,
            "needs_manual_review": True,
        }
        # 不應 raise
        require_approve_for_refund_diff(
            diff=0,
            current_user=APPROVER,
            suggested_total=1500,
            actual_total=1500,
            suggestion=suggestion,
        )


# ── (c) 防迴歸：sessions 已知路徑不受影響 ──────────────────────────────────


class TestRefundDiffRegressionSessionsKnown:
    def test_sessions_known_diff_zero_no_approve_passes(self):
        """sessions 已知，diff=0，一線員工 → 仍不要求簽核（原有行為不變）。"""
        suggestion = {
            "total_suggested_amount": 1500,
            "needs_manual_review": False,
        }
        # 不應 raise
        require_approve_for_refund_diff(
            diff=0,
            current_user=LINE_STAFF,
            suggested_total=1500,
            actual_total=1500,
            suggestion=suggestion,
        )

    def test_sessions_known_diff_below_threshold_no_approve_passes(self):
        """sessions 已知，diff=50（< 門檻），一線員工 → 通過。"""
        suggestion = {
            "total_suggested_amount": 1500,
            "needs_manual_review": False,
        }
        require_approve_for_refund_diff(
            diff=50,
            current_user=LINE_STAFF,
            suggested_total=1500,
            actual_total=1450,
            suggestion=suggestion,
        )

    def test_sessions_known_diff_over_threshold_no_approve_raises_403(self):
        """sessions 已知，diff=200（> 門檻），無 APPROVE → 403（原有行為不變）。"""
        suggestion = {
            "total_suggested_amount": 1500,
            "needs_manual_review": False,
        }
        with pytest.raises(HTTPException) as exc:
            require_approve_for_refund_diff(
                diff=200,
                current_user=LINE_STAFF,
                suggested_total=1500,
                actual_total=1300,
                suggestion=suggestion,
            )
        assert exc.value.status_code == 403

    def test_no_suggestion_kwarg_backward_compat_diff_zero_passes(self):
        """未傳 suggestion 時（backward compat），diff=0 仍不要求簽核。"""
        require_approve_for_refund_diff(
            diff=0,
            current_user=LINE_STAFF,
            suggested_total=1500,
            actual_total=1500,
        )

    def test_no_suggestion_kwarg_backward_compat_diff_over_threshold_blocks(self):
        """未傳 suggestion 時（backward compat），diff > 門檻 + 無 APPROVE → 403。"""
        with pytest.raises(HTTPException) as exc:
            require_approve_for_refund_diff(
                diff=200,
                current_user=LINE_STAFF,
                suggested_total=1500,
                actual_total=1300,
            )
        assert exc.value.status_code == 403
