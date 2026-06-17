"""tests/test_integrations_health_endpoint.py — Phase 4 P1 resilience endpoint tests."""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.integrations_health import router as integrations_health_router
from models.database import Base, User
from utils.auth import hash_password


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "intg-health.sqlite"
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
    app.include_router(integrations_health_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_admin(session, username="ighealth_admin", password="TempPass123"):
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


def _create_noaudit(session, username="ighealth_noaudit", password="TempPass123"):
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


def _login(client, username, password="TempPass123"):
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, f"Login failed: {r.text}"
    # auth uses httpOnly cookies; TestClient auto-stores them for subsequent requests
    return r


def test_requires_audit_logs_permission(client_with_db):
    """無 AUDIT_LOGS 權限 → 403。"""
    client, session_factory = client_with_db
    session = session_factory()
    _create_noaudit(session)
    session.commit()

    _login(client, "ighealth_noaudit")
    r = client.get("/api/internal/integrations/health")
    assert r.status_code == 403


def test_returns_breaker_states(client_with_db):
    """有 AUDIT_LOGS → 200，response 含 3 breaker state 字串。"""
    client, session_factory = client_with_db
    session = session_factory()
    _create_admin(session)
    session.commit()

    _login(client, "ighealth_admin")
    r = client.get("/api/internal/integrations/health")
    assert r.status_code == 200
    data = r.json()

    assert "line" in data
    assert "supabase" in data
    assert "external_http" in data

    assert data["line"]["breaker"] in ("closed", "open", "half_open")
    assert data["supabase"]["breaker"] in ("closed", "open", "half_open")
    assert data["external_http"]["breaker"] in ("closed", "open", "half_open")


def test_counts_pending_uploads(client_with_db):
    """pre-seed 1 pending_upload row → pending_uploads == 1。"""
    client, session_factory = client_with_db
    session = session_factory()
    _create_admin(session)
    session.commit()

    from datetime import datetime, timezone
    from models.pending_uploads import PendingUpload

    row = PendingUpload(
        module="activity_posters",
        key="test/abc.png",
        content_type="image/png",
        local_path="/tmp/fake.bin",
        attempts=0,
        next_retry_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()

    _login(client, "ighealth_admin")
    r = client.get("/api/internal/integrations/health")
    assert r.status_code == 200
    data = r.json()
    assert data["supabase"]["pending_uploads"] == 1
    assert data["supabase"]["final_failed"] == 0


def test_counts_final_failed_uploads(client_with_db):
    """attempts>=5 且未成功的 row → final_failed==1，且不被算進 pending_uploads。"""
    client, session_factory = client_with_db
    session = session_factory()
    _create_admin(session)
    session.commit()

    from datetime import datetime, timezone
    from models.pending_uploads import PendingUpload

    session.add(
        PendingUpload(
            module="activity_posters",
            key="final/x.png",
            content_type="image/png",
            local_path="/tmp/final.bin",
            attempts=5,
            next_retry_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    _login(client, "ighealth_admin")
    r = client.get("/api/internal/integrations/health")
    assert r.status_code == 200
    data = r.json()
    assert data["supabase"]["final_failed"] == 1
    # attempts<5 過濾 → 永久失敗不重複算進「仍可重試」的 pending_uploads
    assert data["supabase"]["pending_uploads"] == 0


def test_no_token_row_returns_null_fields(client_with_db):
    """line_token_health 表為空 → token_healthy=null，consecutive_failures=0。"""
    client, session_factory = client_with_db
    session = session_factory()
    _create_admin(session)
    session.commit()

    _login(client, "ighealth_admin")
    r = client.get("/api/internal/integrations/health")
    assert r.status_code == 200
    data = r.json()
    assert data["line"]["token_healthy"] is None
    assert data["line"]["token_last_check_at"] is None
    assert data["line"]["token_consecutive_failures"] == 0
