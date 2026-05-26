"""api/internal_metrics.py 路由整合測試。

驗證：
- GET /api/internal/metrics 需要 AUDIT_LOGS 權限
- 空 metrics 狀態 → 200，schedulers 為 {}
- scheduler_iteration 累計後 → 200，schedulers 含對應條目
- response 含 worker_pid / hostname 給多 worker 監控聚合
"""

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
from api.internal_metrics import router as internal_metrics_router
from models.database import Base, User
from utils import scheduler_observability as so
from utils.auth import hash_password


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "internal-metrics.sqlite"
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
    so.reset_for_tests()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(internal_metrics_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    so.reset_for_tests()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_admin_with_audit(session, username="metrics_admin", password="TempPass123"):
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


def _login(client, username="metrics_admin", password="TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def test_metrics_requires_audit_logs_permission(client_with_db):
    client, session_factory = client_with_db
    with session_factory() as s:
        user = User(
            username="plain_user",
            password_hash=hash_password("TempPass123"),
            role="staff",
            permission_names=["EMPLOYEES_READ"],
            is_active=True,
        )
        s.add(user)
        s.commit()
    assert _login(client, username="plain_user").status_code == 200

    res = client.get("/api/internal/metrics")
    assert res.status_code == 403


def test_metrics_empty_when_no_scheduler_ran(client_with_db):
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_admin_with_audit(s)
        s.commit()
    assert _login(client).status_code == 200

    res = client.get("/api/internal/metrics")
    assert res.status_code == 200
    body = res.json()
    assert "worker_pid" in body and isinstance(body["worker_pid"], int)
    assert "hostname" in body and isinstance(body["hostname"], str)
    assert body["schedulers"] == {}


def test_metrics_includes_scheduler_after_iteration(client_with_db):
    client, session_factory = client_with_db
    with session_factory() as s:
        _create_admin_with_audit(s)
        s.commit()
    assert _login(client).status_code == 200

    # 模擬一次成功 iteration
    with so.scheduler_iteration("test_sched"):
        so.record_rows("test_sched", 42)

    # 模擬一次失敗
    with so.scheduler_iteration("test_sched"):
        raise RuntimeError("simulated")

    res = client.get("/api/internal/metrics")
    assert res.status_code == 200
    body = res.json()
    schedulers = body["schedulers"]
    assert "test_sched" in schedulers
    entry = schedulers["test_sched"]
    assert entry["last_success_at"] is not None
    assert entry["last_failure_at"] is not None
    assert entry["consecutive_failures"] == 1
    assert entry["total_runs"] == 2
    assert entry["total_failures"] == 1
    assert entry["last_rows_processed"] == 42
    assert entry["total_rows_processed"] == 42
    assert entry["last_error_message"] is not None
    assert "RuntimeError" in entry["last_error_message"]


def test_metrics_unauthenticated_blocked(client_with_db):
    client, _session_factory = client_with_db
    # 未登入直接打 endpoint
    res = client.get("/api/internal/metrics")
    assert res.status_code in (401, 403)
