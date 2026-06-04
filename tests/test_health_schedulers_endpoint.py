"""/health/schedulers endpoint 行為驗證。

無權限端點，UptimeRobot 公開可打。
- 全綠 (lag <= 2 × expected_interval) → 200
- 任一 lagging → 503 + lagging list
- last_success_at IS NULL (啟動後尚未跑過) → 不算 lagging
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.health import router as health_router
from models.scheduler_heartbeat import SchedulerHeartbeat


@pytest.fixture
def health_client(test_db_session):
    """builds a FastAPI client with /health router + sqlite test DB."""
    app = FastAPI()
    app.include_router(health_router)
    with TestClient(app) as client:
        yield client


def test_schedulers_health_all_green(health_client, test_db_session):
    now = datetime.now(timezone.utc)
    test_db_session.add(
        SchedulerHeartbeat(
            scheduler_name="sched_a",
            expected_interval_seconds=300,
            last_success_at=now - timedelta(seconds=60),
        )
    )
    test_db_session.commit()

    r = health_client.get("/health/schedulers")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["total"] == 1
    assert data["lagging_count"] == 0
    # 明細（scheduler 名稱等）不再對未認證端外洩
    assert "schedulers" not in data


def test_schedulers_health_lagging_returns_503(health_client, test_db_session):
    now = datetime.now(timezone.utc)
    test_db_session.add(
        SchedulerHeartbeat(
            scheduler_name="sched_b",
            expected_interval_seconds=300,
            last_success_at=now - timedelta(seconds=900),  # > 2 * 300
        )
    )
    test_db_session.commit()

    r = health_client.get("/health/schedulers")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "degraded"
    assert data["lagging_count"] == 1
    # 明細（名稱/lag）改記 server log，不對未認證端外洩
    assert "lagging" not in data


def test_schedulers_health_never_ran_not_lagging(health_client, test_db_session):
    """啟動後尚未 tick：last_success_at IS NULL → 不算 lagging。"""
    test_db_session.add(
        SchedulerHeartbeat(
            scheduler_name="sched_c",
            expected_interval_seconds=300,
            last_success_at=None,
        )
    )
    test_db_session.commit()

    r = health_client.get("/health/schedulers")
    assert r.status_code == 200
    data = r.json()
    # last_success_at IS NULL 不算 lagging
    assert data["status"] == "ok"
    assert data["lagging_count"] == 0


def test_schedulers_health_mixed_green_and_lagging(health_client, test_db_session):
    now = datetime.now(timezone.utc)
    test_db_session.add(
        SchedulerHeartbeat(
            scheduler_name="sched_green",
            expected_interval_seconds=300,
            last_success_at=now - timedelta(seconds=120),
        )
    )
    test_db_session.add(
        SchedulerHeartbeat(
            scheduler_name="sched_lag",
            expected_interval_seconds=300,
            last_success_at=now - timedelta(seconds=1200),
        )
    )
    test_db_session.commit()

    r = health_client.get("/health/schedulers")
    assert r.status_code == 503
    data = r.json()
    assert data["total"] == 2
    assert data["lagging_count"] == 1  # 只有 sched_lag lagging


def test_schedulers_health_empty_returns_ok(health_client):
    """heartbeat 表空（極早期啟動）→ 200 ok 不告警。"""
    r = health_client.get("/health/schedulers")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
