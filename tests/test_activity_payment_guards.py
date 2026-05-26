"""tests/test_activity_payment_guards.py — activity_payment_guards 測試。"""

import os
import sys
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.activity import (  # noqa: F401 metadata
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
)
from models.base import Base
from models.classroom import Classroom, Student  # noqa: F401 metadata
from services.activity_payment_guards import (
    has_payment_approve,
    require_approve_for_cumulative_refund,
    require_approve_for_high_price,
    require_approve_for_large_refund,
    require_refund_reason,
)
from utils.activity_constants import (
    ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD,
    MIN_REFUND_REASON_LENGTH,
    REFUND_APPROVAL_THRESHOLD,
)
from utils.permissions import Permission

APPROVER = {"permission_names": ["ACTIVITY_PAYMENT_APPROVE"]}
LINE_STAFF = {"permission_names": []}
ALL_PERMS = {"permission_names": ["*"]}


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _make_registration(s):
    c = Classroom(name="小一", is_active=True)
    s.add(c)
    s.flush()
    st = Student(student_id="S001", name="阿明", classroom_id=c.id)
    s.add(st)
    s.flush()
    course = ActivityCourse(name="圍棋", price=1000, capacity=30, is_active=True)
    s.add(course)
    s.flush()
    reg = ActivityRegistration(student_id=st.id, student_name="阿明")
    s.add(reg)
    s.flush()
    return reg


def _add_refund(s, reg_id, amount, *, voided=False):
    rec = ActivityPaymentRecord(
        registration_id=reg_id,
        type="refund",
        amount=amount,
        payment_date=date(2026, 5, 17),
    )
    s.add(rec)
    s.flush()
    if voided:
        from datetime import datetime as dt

        rec.voided_at = dt.now()
        s.flush()
    return rec


class TestHasPaymentApprove:
    def test_user_with_bit_returns_true(self):
        assert has_payment_approve(APPROVER) is True

    def test_user_without_bit_returns_false(self):
        assert has_payment_approve(LINE_STAFF) is False

    def test_admin_all_perms_returns_true(self):
        assert has_payment_approve(ALL_PERMS) is True

    def test_missing_permissions_key_returns_false(self):
        assert has_payment_approve({}) is False


class TestRequireRefundReason:
    def test_long_enough_returns_cleaned(self):
        notes = "  " + "X" * MIN_REFUND_REASON_LENGTH + "  "
        result = require_refund_reason(notes)
        assert result == "X" * MIN_REFUND_REASON_LENGTH

    def test_too_short_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            require_refund_reason("太短")
        assert exc.value.status_code == 400

    def test_none_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            require_refund_reason(None)
        assert exc.value.status_code == 400

    def test_empty_string_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            require_refund_reason("")
        assert exc.value.status_code == 400

    def test_only_whitespace_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            require_refund_reason("    ")
        assert exc.value.status_code == 400


class TestRequireApproveForHighPrice:
    def test_at_threshold_does_not_raise(self):
        # amount > threshold 才檢查，等於 threshold 通過
        require_approve_for_high_price(ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD, LINE_STAFF)

    def test_over_threshold_without_perm_raises_403(self):
        with pytest.raises(HTTPException) as exc:
            require_approve_for_high_price(
                ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD + 1, LINE_STAFF
            )
        assert exc.value.status_code == 403

    def test_over_threshold_with_perm_passes(self):
        require_approve_for_high_price(
            ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD + 5000, APPROVER
        )

    def test_label_appears_in_detail(self):
        with pytest.raises(HTTPException) as exc:
            require_approve_for_high_price(
                ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD + 1,
                LINE_STAFF,
                label="圍棋報名費",
            )
        assert "圍棋報名費" in exc.value.detail


class TestRequireApproveForLargeRefund:
    def test_at_threshold_does_not_raise(self):
        require_approve_for_large_refund(REFUND_APPROVAL_THRESHOLD, LINE_STAFF)

    def test_over_threshold_without_perm_raises_403(self):
        with pytest.raises(HTTPException) as exc:
            require_approve_for_large_refund(REFUND_APPROVAL_THRESHOLD + 1, LINE_STAFF)
        assert exc.value.status_code == 403

    def test_over_threshold_with_perm_passes(self):
        require_approve_for_large_refund(REFUND_APPROVAL_THRESHOLD + 500, APPROVER)

    def test_small_refund_anyone_passes(self):
        require_approve_for_large_refund(100, LINE_STAFF)


class TestRequireApproveForCumulativeRefund:
    def test_no_prior_refund_uses_this_amount_only(self, session):
        reg = _make_registration(session)
        # 本次 500 < threshold → 通過
        require_approve_for_cumulative_refund(
            session, reg.id, 500, LINE_STAFF, label="累積退費"
        )

    def test_prior_plus_this_over_threshold_raises_403(self, session):
        reg = _make_registration(session)
        _add_refund(session, reg.id, 600)
        # 本次再退 500 → 累積 1100 > 1000，無權限應 403
        with pytest.raises(HTTPException) as exc:
            require_approve_for_cumulative_refund(
                session, reg.id, 500, LINE_STAFF, label="累積退費總額"
            )
        assert exc.value.status_code == 403

    def test_voided_refunds_excluded_from_sum(self, session):
        reg = _make_registration(session)
        _add_refund(session, reg.id, 800, voided=True)  # 不計入
        # voided 不算累積，本次 500 → 累積 500 < 1000 通過
        require_approve_for_cumulative_refund(
            session, reg.id, 500, LINE_STAFF, label="累積退費"
        )

    def test_approver_passes_even_when_cumulative_exceeds(self, session):
        reg = _make_registration(session)
        _add_refund(session, reg.id, 900)
        # 累積 1500 > threshold，但 APPROVER 有權限
        require_approve_for_cumulative_refund(
            session, reg.id, 600, APPROVER, label="累積退費"
        )


# ── require_approve_for_refund_diff: 偏離 calculator 建議值簽核 ─────────────


from services.activity_payment_guards import require_approve_for_refund_diff


def test_refund_diff_below_threshold_passes():
    """diff <= NT$100 → 任何員工通過。"""
    # 應該不 raise
    require_approve_for_refund_diff(
        diff=50,
        current_user=LINE_STAFF,
        suggested_total=500,
        actual_total=550,
    )


def test_refund_diff_at_threshold_passes():
    """diff == threshold 邊界 → 視為 ≤ pass。"""
    require_approve_for_refund_diff(
        diff=100,
        current_user=LINE_STAFF,
        suggested_total=500,
        actual_total=600,
    )


def test_refund_diff_over_threshold_blocks_staff():
    """diff > NT$100 + 一線員工 → 403。"""
    with pytest.raises(HTTPException) as exc:
        require_approve_for_refund_diff(
            diff=101,
            current_user=LINE_STAFF,
            suggested_total=500,
            actual_total=601,
        )
    assert exc.value.status_code == 403
    assert "偏離" in exc.value.detail or "差" in exc.value.detail


def test_refund_diff_over_threshold_passes_approver():
    """diff > NT$100 + ACTIVITY_PAYMENT_APPROVE → pass。"""
    require_approve_for_refund_diff(
        diff=500,
        current_user=APPROVER,
        suggested_total=500,
        actual_total=1000,
    )


def test_refund_diff_error_message_contains_amounts():
    """403 detail 應含 suggested / actual / diff 三個金額方便員工 debug。"""
    with pytest.raises(HTTPException) as exc:
        require_approve_for_refund_diff(
            diff=200,
            current_user=LINE_STAFF,
            suggested_total=800,
            actual_total=1000,
        )
    msg = exc.value.detail
    assert "800" in msg and "1000" in msg and "200" in msg
