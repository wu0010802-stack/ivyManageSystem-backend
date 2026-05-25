"""Test /audit-logs/high-risk + ack endpoints"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.audit import router as audit_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import AuditLog, Base, User
from utils.auth import hash_password


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "audit-high-risk.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(audit_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_admin_user(session, username="hr_admin", password="TempPass123"):
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permission_names=["AUDIT_LOGS"],
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _create_viewer_user(session, username="plain_viewer", password="TempPass123"):
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="staff",
        permission_names=["EMPLOYEES_READ"],
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username="hr_admin", password="TempPass123"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, f"Login failed: {res.text}"
    return res


# ============== GET /audit-logs/high-risk ==============


def test_get_high_risk_requires_audit_logs_permission(client_with_db):
    """無 AUDIT_LOGS 權限 → 403"""
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_viewer_user(s)
        s.commit()
    _login(client, username="plain_viewer")

    res = client.get("/api/audit-logs/high-risk")
    assert res.status_code == 403


def test_get_high_risk_returns_unack_only_by_default(client_with_db):
    """預設 unack_only=True，已 ack 不回傳"""
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_admin_user(s)
        unacked = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="刪除 X (不可復原)",
            username="admin",
        )
        acked = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="刪除 Y (不可復原)",
            username="admin",
            acknowledged_at=datetime.now(timezone.utc),
            acknowledged_by=1,
        )
        s.add_all([unacked, acked])
        s.commit()
        unacked_id = unacked.id
        acked_id = acked.id

    _login(client)
    res = client.get("/api/audit-logs/high-risk")
    assert res.status_code == 200
    data = res.json()
    ids = {item["id"] for item in data["items"]}
    assert unacked_id in ids
    assert acked_id not in ids
    assert data["unack_count"] >= 1


def test_get_high_risk_with_unack_only_false(client_with_db):
    """unack_only=false → 包含已 ack"""
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_admin_user(s)
        unacked = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="刪除 A (不可復原)",
            username="admin",
        )
        acked = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="刪除 B (不可復原)",
            username="admin",
            acknowledged_at=datetime.now(timezone.utc),
            acknowledged_by=1,
        )
        s.add_all([unacked, acked])
        s.commit()
        unacked_id = unacked.id
        acked_id = acked.id

    _login(client)
    res = client.get("/api/audit-logs/high-risk?unack_only=false")
    assert res.status_code == 200
    data = res.json()
    ids = {item["id"] for item in data["items"]}
    assert unacked_id in ids
    assert acked_id in ids


def test_get_high_risk_respects_days_param(client_with_db):
    """8 天前的 row 在 days=7 不該回傳"""
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_admin_user(s)
        old_row = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="刪除老的 (不可復原)",
            username="admin",
            created_at=datetime.now(timezone.utc) - timedelta(days=8),
        )
        s.add(old_row)
        s.commit()
        old_id = old_row.id

    _login(client)
    res = client.get("/api/audit-logs/high-risk?days=7")
    assert res.status_code == 200
    data = res.json()
    assert old_id not in {item["id"] for item in data["items"]}


def test_get_high_risk_classifies_risk_kind(client_with_db):
    """item.risk_kind 三類各一筆能正確派分"""
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_admin_user(s)
        hd = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="刪 (不可復原)",
            username="a",
        )
        bl = AuditLog(
            action="BLOCKED_DELETE",
            entity_type="employee",
            summary="拒絕刪",
            username="a",
        )
        pc = AuditLog(
            action="UPDATE",
            entity_type="user",
            summary="改 role: hr → admin",
            username="a",
        )
        s.add_all([hd, bl, pc])
        s.commit()
        hd_id = hd.id
        bl_id = bl.id
        pc_id = pc.id

    _login(client)
    res = client.get("/api/audit-logs/high-risk?days=7")
    assert res.status_code == 200
    items = {item["id"]: item["risk_kind"] for item in res.json()["items"]}
    assert items[hd_id] == "hard_delete"
    assert items[bl_id] == "blocked"
    assert items[pc_id] == "permission_change"


# ============== POST /audit-logs/{id}/ack ==============


def test_ack_single_marks_acknowledged(client_with_db):
    client, session_factory = client_with_db
    with session_factory() as s:
        admin = _create_admin_user(s)
        s.commit()
        admin_id = admin.id

    _login(client)

    with session_factory() as s:
        row = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="刪 (不可復原)",
            username="admin",
        )
        s.add(row)
        s.commit()
        row_id = row.id

    res = client.post(f"/api/audit-logs/{row_id}/ack")
    assert res.status_code == 200

    with session_factory() as s:
        refreshed = s.get(AuditLog, row_id)
        assert refreshed.acknowledged_at is not None
        assert refreshed.acknowledged_by == admin_id


def test_ack_returns_404_for_missing(client_with_db):
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_admin_user(s)
        s.commit()
    _login(client)
    res = client.post("/api/audit-logs/999999/ack")
    assert res.status_code == 404


def test_ack_is_idempotent(client_with_db):
    """重複 ack 同筆 → timestamp / user 維持第一次"""
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_admin_user(s)
        s.commit()

    _login(client)

    with session_factory() as s:
        row = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="x (不可復原)",
            username="admin",
        )
        s.add(row)
        s.commit()
        row_id = row.id

    client.post(f"/api/audit-logs/{row_id}/ack")

    with session_factory() as s:
        r = s.get(AuditLog, row_id)
        first_at = r.acknowledged_at
        first_by = r.acknowledged_by

    client.post(f"/api/audit-logs/{row_id}/ack")

    with session_factory() as s:
        r = s.get(AuditLog, row_id)
        assert r.acknowledged_at == first_at
        assert r.acknowledged_by == first_by


def test_ack_does_not_create_audit_log(client_with_db):
    """ack 動作本身不寫新 audit log"""
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_admin_user(s)
        row = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="x (不可復原)",
            username="admin",
        )
        s.add(row)
        s.commit()
        row_id = row.id

    _login(client)

    with session_factory() as s:
        before = s.query(AuditLog).count()

    client.post(f"/api/audit-logs/{row_id}/ack")

    with session_factory() as s:
        after = s.query(AuditLog).count()
    assert after == before, "ack endpoint 不該自己產生 audit log"


def test_ack_requires_audit_logs_permission(client_with_db):
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_viewer_user(s)
        row = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="x (不可復原)",
            username="admin",
        )
        s.add(row)
        s.commit()
        row_id = row.id

    _login(client, username="plain_viewer")
    res = client.post(f"/api/audit-logs/{row_id}/ack")
    assert res.status_code == 403


# ============== POST /audit-logs/ack-all ==============


def test_ack_all_marks_only_unack_in_window(client_with_db):
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_admin_user(s)
        unacked = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="a (不可復原)",
            username="x",
        )
        already_acked = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="b (不可復原)",
            username="x",
            acknowledged_at=datetime.now(timezone.utc),
            acknowledged_by=99,
        )
        s.add_all([unacked, already_acked])
        s.commit()
        unacked_id = unacked.id
        already_acked_id = already_acked.id
        original_already_ack_at = already_acked.acknowledged_at

    _login(client)
    res = client.post("/api/audit-logs/ack-all?days=7")
    assert res.status_code == 200

    with session_factory() as s:
        u = s.get(AuditLog, unacked_id)
        a = s.get(AuditLog, already_acked_id)
        assert u.acknowledged_at is not None
        assert a.acknowledged_at == original_already_ack_at, "已 ack 不該被重寫"


def test_ack_all_returns_count(client_with_db):
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_admin_user(s)
        unacked_a = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="a (不可復原)",
            username="x",
        )
        unacked_b = AuditLog(
            action="BLOCKED_DELETE", entity_type="user", summary="拒絕", username="x"
        )
        s.add_all([unacked_a, unacked_b])
        s.commit()

    _login(client)
    res = client.post("/api/audit-logs/ack-all?days=7")
    assert res.status_code == 200
    body = res.json()
    assert body["acknowledged_count"] >= 2
