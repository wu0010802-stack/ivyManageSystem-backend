"""tests/test_audit_log_gc.py — P0b audit_log retention GC 測試。

Refs: docs/superpowers/specs/2026-05-28-audit-pii-redact-retention-design.md §4.3
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from models.audit import AuditLog
from models.base import Base
from utils.audit_log_gc import (
    _AUTH_DAYS,
    _FALLBACK_DAYS,
    _FINANCE_DAYS,
    _STUDENT_DAYS,
    _retention_days_for,
    cleanup_audit_logs,
)
from utils.taipei_time import now_taipei_naive

# ── retention 對照表 ──


def test_retention_days_finance():
    assert _retention_days_for("salary") == _FINANCE_DAYS
    assert _retention_days_for("fee") == _FINANCE_DAYS
    assert _retention_days_for("salary_record") == _FINANCE_DAYS
    assert _retention_days_for("vendor_payment") == _FINANCE_DAYS


def test_retention_days_auth():
    assert _retention_days_for("auth") == _AUTH_DAYS


def test_retention_days_student():
    assert _retention_days_for("student") == _STUDENT_DAYS
    assert _retention_days_for("employee") == _STUDENT_DAYS
    assert _retention_days_for("attendance") == _STUDENT_DAYS
    assert _retention_days_for("medical") == _STUDENT_DAYS


def test_retention_days_fallback():
    assert _retention_days_for("unknown_type") == _FALLBACK_DAYS
    assert _retention_days_for("calendar_event") == _FALLBACK_DAYS


# ── cleanup_audit_logs SQL 行為（用 SQLite in-memory）──


@pytest.fixture()
def session():
    """SQLite in-memory session（不污染 dev DB）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    AuditLog.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _insert_log(session, entity_type: str, days_ago: int):
    log = AuditLog(
        user_id=1,
        username="x",
        action="UPDATE",
        entity_type=entity_type,
        entity_id="1",
        summary="test",
        ip_address="1.2.3.4",
        created_at=now_taipei_naive() - timedelta(days=days_ago),
    )
    session.add(log)
    session.commit()
    return log.id


def test_finance_log_older_than_7y_deleted(session):
    _insert_log(session, "salary", days_ago=_FINANCE_DAYS + 1)
    deleted = cleanup_audit_logs(session)
    assert deleted == 1
    assert session.query(AuditLog).count() == 0


def test_finance_log_within_7y_kept(session):
    _insert_log(session, "salary", days_ago=_FINANCE_DAYS - 30)
    deleted = cleanup_audit_logs(session)
    assert deleted == 0
    assert session.query(AuditLog).count() == 1


def test_auth_log_older_than_6m_deleted(session):
    _insert_log(session, "auth", days_ago=_AUTH_DAYS + 1)
    deleted = cleanup_audit_logs(session)
    assert deleted == 1


def test_auth_log_within_6m_kept(session):
    _insert_log(session, "auth", days_ago=_AUTH_DAYS - 10)
    deleted = cleanup_audit_logs(session)
    assert deleted == 0


def test_student_log_older_than_3y_deleted(session):
    _insert_log(session, "student", days_ago=_STUDENT_DAYS + 1)
    deleted = cleanup_audit_logs(session)
    assert deleted == 1


def test_unknown_entity_type_uses_fallback_3y(session):
    _insert_log(session, "custom_type_xyz", days_ago=_FALLBACK_DAYS + 1)
    deleted = cleanup_audit_logs(session)
    assert deleted == 1


def test_mixed_entity_types(session):
    """不同 entity_type 按各自 retention 刪。"""
    _insert_log(session, "salary", days_ago=_FINANCE_DAYS + 1)  # 刪
    _insert_log(session, "salary", days_ago=100)  # 保
    _insert_log(session, "auth", days_ago=_AUTH_DAYS + 1)  # 刪
    _insert_log(session, "auth", days_ago=10)  # 保
    _insert_log(session, "student", days_ago=_STUDENT_DAYS + 1)  # 刪

    deleted = cleanup_audit_logs(session)
    assert deleted == 3
    assert session.query(AuditLog).count() == 2


def test_empty_table_no_op(session):
    deleted = cleanup_audit_logs(session)
    assert deleted == 0
