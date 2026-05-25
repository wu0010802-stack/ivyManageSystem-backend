"""Test audit_high_risk service: filter_high_risk + classify_risk_kind"""

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from models.audit import AuditLog
from services.audit_high_risk import (
    HIGH_RISK_ACTIONS,
    filter_high_risk,
    classify_risk_kind,
)

# ============== classify_risk_kind ==============


def test_classify_http_delete_as_hard_delete():
    row = AuditLog(
        action="DELETE", entity_type="employee", summary="刪除員工 X (不可復原)"
    )
    assert classify_risk_kind(row) == "hard_delete"


def test_classify_marker_only_hard_delete():
    """非 HTTP DELETE 但 summary 含 (不可復原) 也算 hard_delete"""
    row = AuditLog(
        action="UPDATE", entity_type="employee", summary="真刪 員工 X (不可復原)"
    )
    assert classify_risk_kind(row) == "hard_delete"


def test_classify_blocked_action():
    row = AuditLog(action="BLOCKED_DELETE", entity_type="employee", summary="拒絕")
    assert classify_risk_kind(row) == "blocked"


def test_classify_blocked_create():
    row = AuditLog(action="BLOCKED_CREATE", entity_type="user", summary="拒絕")
    assert classify_risk_kind(row) == "blocked"


def test_classify_permission_change_fallback():
    row = AuditLog(
        action="UPDATE",
        entity_type="user",
        summary="修改使用者 jdoe (role: hr → admin)",
    )
    assert classify_risk_kind(row) == "permission_change"


# ============== filter_high_risk ==============


def _make_row(
    session,
    *,
    action,
    entity_type="employee",
    summary="x",
    acked=False,
    created_offset=timedelta(),
):
    row = AuditLog(
        action=action,
        entity_type=entity_type,
        summary=summary,
        username="admin",
        created_at=datetime.now(timezone.utc) + created_offset,
    )
    if acked:
        row.acknowledged_at = datetime.now(timezone.utc)
        row.acknowledged_by = 1
    session.add(row)
    session.commit()
    return row


def test_filter_includes_http_delete(test_db_session):
    row = _make_row(test_db_session, action="DELETE", summary="刪除員工 X (不可復原)")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    query = filter_high_risk(sa.select(AuditLog), since=since)
    results = test_db_session.execute(query).scalars().all()
    assert row.id in [r.id for r in results]


def test_filter_includes_blocked(test_db_session):
    row = _make_row(test_db_session, action="BLOCKED_DELETE")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = (
        test_db_session.execute(filter_high_risk(sa.select(AuditLog), since=since))
        .scalars()
        .all()
    )
    assert row.id in [r.id for r in results]


def test_filter_includes_marker_only_hard_delete(test_db_session):
    """非 HTTP DELETE 但 summary 含「(不可復原)」要被 catch"""
    row = _make_row(test_db_session, action="UPDATE", summary="真刪 員工 X (不可復原)")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = (
        test_db_session.execute(filter_high_risk(sa.select(AuditLog), since=since))
        .scalars()
        .all()
    )
    assert row.id in [r.id for r in results]


def test_filter_includes_permission_change(test_db_session):
    """user entity + summary 含 role/permission 字眼"""
    row = _make_row(
        test_db_session,
        action="UPDATE",
        entity_type="user",
        summary="修改使用者 (role: hr → admin)",
    )
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = (
        test_db_session.execute(filter_high_risk(sa.select(AuditLog), since=since))
        .scalars()
        .all()
    )
    assert row.id in [r.id for r in results]


def test_filter_excludes_normal_update(test_db_session):
    """普通 UPDATE 不該被 catch"""
    row = _make_row(
        test_db_session, action="UPDATE", entity_type="employee", summary="修改員工資料"
    )
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = (
        test_db_session.execute(filter_high_risk(sa.select(AuditLog), since=since))
        .scalars()
        .all()
    )
    assert row.id not in [r.id for r in results]


def test_filter_excludes_acked_when_unack_only(test_db_session):
    """acked 已標 + only_unack=True → 不該回傳"""
    row = _make_row(
        test_db_session, action="DELETE", summary="x (不可復原)", acked=True
    )
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = (
        test_db_session.execute(
            filter_high_risk(sa.select(AuditLog), since=since, only_unack=True)
        )
        .scalars()
        .all()
    )
    assert row.id not in [r.id for r in results]


def test_filter_includes_acked_when_only_unack_false(test_db_session):
    row = _make_row(
        test_db_session, action="DELETE", summary="x (不可復原)", acked=True
    )
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = (
        test_db_session.execute(
            filter_high_risk(sa.select(AuditLog), since=since, only_unack=False)
        )
        .scalars()
        .all()
    )
    assert row.id in [r.id for r in results]


def test_filter_respects_time_window(test_db_session):
    """8 天前的 row 不該被 7 天窗 catch"""
    row = _make_row(
        test_db_session,
        action="DELETE",
        summary="x (不可復原)",
        created_offset=timedelta(days=-8),
    )
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = (
        test_db_session.execute(filter_high_risk(sa.select(AuditLog), since=since))
        .scalars()
        .all()
    )
    assert row.id not in [r.id for r in results]


def test_high_risk_actions_constant():
    assert HIGH_RISK_ACTIONS == {
        "DELETE",
        "BLOCKED_CREATE",
        "BLOCKED_UPDATE",
        "BLOCKED_DELETE",
    }
