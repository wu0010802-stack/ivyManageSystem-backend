"""set_lifecycle_status：原子化 lifecycle 變更 + terminal_entered_at + audit_log。"""

import uuid
from datetime import datetime, timezone
import pytest

from models.classroom import (
    Student,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_ON_LEAVE,
)
from models.audit import AuditLog
from utils.student_lifecycle import set_lifecycle_status


def _make_student(session, *, lifecycle=LIFECYCLE_ACTIVE, terminal_at=None):
    s = Student(
        student_id=str(uuid.uuid4())[:20],  # 唯一學號（NOT NULL + UNIQUE）
        name="測試",
        lifecycle_status=lifecycle,
        terminal_entered_at=terminal_at,
    )
    session.add(s)
    session.flush()
    return s


def test_set_active_to_graduated_sets_terminal_entered_at(test_db_session):
    s = _make_student(test_db_session, lifecycle=LIFECYCLE_ACTIVE)
    before = datetime.now(timezone.utc)
    set_lifecycle_status(test_db_session, s, LIFECYCLE_GRADUATED, actor_user_id=1)
    assert s.lifecycle_status == LIFECYCLE_GRADUATED
    assert s.terminal_entered_at is not None
    # 允許 aware/naive 比較：把 naive 當 UTC
    ts = s.terminal_entered_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    assert ts >= before


def test_set_graduated_back_to_active_clears_terminal_entered_at(test_db_session):
    s = _make_student(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        terminal_at=datetime.now(timezone.utc),
    )
    set_lifecycle_status(test_db_session, s, LIFECYCLE_ACTIVE, actor_user_id=1)
    assert s.lifecycle_status == LIFECYCLE_ACTIVE
    assert s.terminal_entered_at is None


def test_set_terminal_to_terminal_keeps_timestamp(test_db_session):
    """從一個終態換到另一個終態，原戳記不動（避免 retention timer reset）。"""
    fixed = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _make_student(
        test_db_session, lifecycle=LIFECYCLE_TRANSFERRED, terminal_at=fixed
    )
    set_lifecycle_status(test_db_session, s, LIFECYCLE_GRADUATED, actor_user_id=1)
    assert s.lifecycle_status == LIFECYCLE_GRADUATED
    assert s.terminal_entered_at == fixed


def test_same_status_no_op(test_db_session):
    s = _make_student(test_db_session, lifecycle=LIFECYCLE_ACTIVE)
    audit_count_before = test_db_session.query(AuditLog).count()
    set_lifecycle_status(test_db_session, s, LIFECYCLE_ACTIVE, actor_user_id=1)
    assert test_db_session.query(AuditLog).count() == audit_count_before


def test_audit_log_written(test_db_session):
    s = _make_student(test_db_session, lifecycle=LIFECYCLE_ACTIVE)
    set_lifecycle_status(
        test_db_session, s, LIFECYCLE_ON_LEAVE, actor_user_id=42, reason="家長申請"
    )
    test_db_session.flush()

    log = (
        test_db_session.query(AuditLog)
        .filter(
            AuditLog.entity_type == "student",
            AuditLog.entity_id == str(s.id),
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert log is not None
    assert log.action == "UPDATE"
    assert log.user_id == 42
    assert "on_leave" in log.changes
    assert "家長申請" in log.changes


def test_audit_disabled_when_audit_false(test_db_session):
    s = _make_student(test_db_session, lifecycle=LIFECYCLE_ACTIVE)
    audit_count_before = test_db_session.query(AuditLog).count()
    set_lifecycle_status(
        test_db_session, s, LIFECYCLE_GRADUATED, actor_user_id=1, audit=False
    )
    test_db_session.flush()
    assert test_db_session.query(AuditLog).count() == audit_count_before
