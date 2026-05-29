"""
Spec E P0 #6：LINE 推播跨境合規 consent gate 測試

涵蓋：
1. _check_line_push_consent：3 個 DB-level case + 1 DB error case
2. build_*_message 去識別化：2 個代表性 case
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

from services.line_service import (
    _check_line_push_consent,
    build_activity_waitlist_promoted_message,
    build_dismissal_message,
)

# ── _check_line_push_consent ──────────────────────────────────────────────────


def test_check_line_push_consent_user_not_bound_returns_false(test_db_session):
    """LINE user_id 未綁定任何 User → 回傳 False（fail-closed）。"""
    result = _check_line_push_consent("U_nonexistent_line_id")
    assert result is False


def test_check_line_push_consent_consent_false_returns_false(test_db_session):
    """User 已綁定 LINE 但 line_push_consent=False → 回傳 False。"""
    from models.auth import User

    user = User(
        username="test_no_consent",
        password_hash="hashed",
        role="teacher",
        line_user_id="U_consent_false_001",
        line_push_consent=False,
    )
    test_db_session.add(user)
    test_db_session.commit()

    result = _check_line_push_consent("U_consent_false_001")
    assert result is False


def test_check_line_push_consent_consent_true_returns_true(test_db_session):
    """User 已綁定 LINE 且 line_push_consent=True → 回傳 True。"""
    from models.auth import User

    user = User(
        username="test_with_consent",
        password_hash="hashed",
        role="teacher",
        line_user_id="U_consent_true_001",
        line_push_consent=True,
    )
    test_db_session.add(user)
    test_db_session.commit()

    result = _check_line_push_consent("U_consent_true_001")
    assert result is True


def test_check_line_push_consent_db_error_returns_false():
    """session_scope 拋出例外 → fail-closed 回傳 False（不 re-raise）。"""
    with patch("models.base.session_scope") as mock_scope:
        mock_scope.side_effect = RuntimeError("DB connection lost")
        result = _check_line_push_consent("U_any_user_id")
    assert result is False


# ── build_*_message 去識別化 ──────────────────────────────────────────────────


def test_build_activity_waitlist_promoted_message_no_student_name():
    """build_activity_waitlist_promoted_message 不再 inline student_name。

    原簽章保留（backward-compat），但輸出訊息改用「您的孩子」，不含傳入的名字。
    """
    msg = build_activity_waitlist_promoted_message(
        student_name="王小明",
        course_name="兒童鋼琴班",
        deadline=datetime(2026, 6, 1, 12, 0),
    )
    assert "王小明" not in msg, "student_name 不應出現在訊息中（去識別化）"
    assert "您的孩子" in msg
    assert "兒童鋼琴班" in msg


def test_build_dismissal_message_no_classroom_name():
    """build_dismissal_message 不再 inline student_name / classroom_name。

    原簽章保留（backward-compat），但輸出改用「您的孩子已可接送」，不含名字或班級。
    """
    msg = build_dismissal_message(
        student_name="陳小花",
        classroom_name="大班A",
        note="請走正門",
    )
    assert "陳小花" not in msg, "student_name 不應出現在訊息中（去識別化）"
    assert "大班A" not in msg, "classroom_name 不應出現在訊息中（去識別化）"
    assert "您的孩子" in msg
    assert "請走正門" in msg  # note 是非 PII，應保留
