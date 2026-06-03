"""PII Retention GC 測試：retention 邊界、dry-run、SKIP LOCKED、idempotency。"""

from datetime import datetime, timedelta, timezone

import pytest

from models.audit import AuditLog
from models.classroom import Student, LIFECYCLE_ACTIVE, LIFECYCLE_GRADUATED
from models.guardian import Guardian
from services.pii_retention_scheduler import _run_pii_retention_gc

_counter = 0


def _make_guardian_pair(
    session, *, lifecycle, days_ago, user_id=None, pii_redacted=False
):
    """建一對 student+guardian，方便重複用。"""
    global _counter
    _counter += 1
    student = Student(
        student_id=f"TEST-{_counter:04d}",
        name="畢業生",
        birthday=None,
        lifecycle_status=lifecycle,
        terminal_entered_at=(
            datetime.now(timezone.utc) - timedelta(days=days_ago)
            if lifecycle != LIFECYCLE_ACTIVE
            else None
        ),
    )
    session.add(student)
    session.flush()
    g = Guardian(
        student_id=student.id,
        user_id=user_id,
        name="王媽媽",
        phone="0912345678",
        email="mom@example.com",
        relation="母親",
        custody_note="探視週末",
        pii_redacted_at=(datetime.now(timezone.utc) if pii_redacted else None),
    )
    session.add(g)
    session.flush()
    return student, g


def test_gc_redacts_after_365_days(test_db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=7,
    )
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(g)
    assert g.name == "[已離校家長]"
    assert g.phone is None
    assert g.email is None
    assert g.relation is None
    assert g.custody_note is None
    assert g.user_id is None
    assert g.pii_redacted_at is not None


def test_gc_redacts_student_parent_snapshot(test_db_session, monkeypatch):
    """GC 抹 Guardian 時，也要抹 students 表上的去正規化家長快照（parent_name/phone）。

    對稱補抹：否則同一份家長 PII 以明文續存於 students（雙寫副本），等同 GC 被繞過。
    """
    # 直接 patch dry_run_enabled：test_db_session setup 已先觸發 get_settings() 快取
    # dry_run=True（預設安全值），此時測試體內 setenv 已太晚。patch 確保關閉 dry-run。
    monkeypatch.setattr(
        "services.pii_retention_scheduler.dry_run_enabled", lambda: False
    )
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=7,
    )
    student.parent_name = "王媽媽"
    student.parent_phone = "0912345678"
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(student)
    assert student.parent_name == "[已離校家長]"
    assert student.parent_phone is None


def test_gc_skips_within_retention_window(test_db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=300,
        user_id=7,
    )
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(g)
    assert g.name == "王媽媽"
    assert g.phone == "0912345678"
    assert g.pii_redacted_at is None


def test_gc_skips_active_students(test_db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_ACTIVE,
        days_ago=400,
        user_id=7,
    )
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(g)
    assert g.phone == "0912345678"


def test_dry_run_does_not_modify(test_db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "1")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=7,
    )
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(g)
    assert g.phone == "0912345678"
    assert g.pii_redacted_at is None


def test_gc_idempotent_skips_already_redacted(test_db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=7,
        pii_redacted=True,
    )
    initial_redacted_at = g.pii_redacted_at
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(g)
    # SQLite 存回時會剝 tzinfo；比較 naive 值即可（語意是「未被動過」）
    refreshed = g.pii_redacted_at
    if refreshed is not None and refreshed.tzinfo is None:
        initial_naive = initial_redacted_at.replace(tzinfo=None)
    else:
        initial_naive = initial_redacted_at
    assert refreshed == initial_naive


def test_gc_writes_audit_log_without_pii(test_db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=7,
    )
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    log = (
        test_db_session.query(AuditLog)
        .filter(
            AuditLog.entity_type == "guardian",
            AuditLog.entity_id == str(g.id),
            AuditLog.username == "pii_retention_gc",
        )
        .first()
    )
    assert log is not None
    assert "0912345678" not in (log.changes or "")
    assert "mom@example.com" not in (log.changes or "")
    assert "王媽媽" not in (log.changes or "")
    assert str(student.id) in log.changes


def test_gc_redact_unlinks_user_so_parent_portal_returns_empty(
    test_db_session, monkeypatch
):
    """user_id 解綁後 _get_parent_student_ids 回空 list（家長 portal 看不到）。"""
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=99,
    )
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    from api.parent_portal._shared import _get_parent_student_ids

    test_db_session.expire_all()
    guardian_ids, student_ids = _get_parent_student_ids(test_db_session, 99)
    assert guardian_ids == []
    assert student_ids == []
