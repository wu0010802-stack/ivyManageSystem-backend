"""tests/test_health_deep.py — Ch3 deep /health/ready 行為驗證。"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def health_client():
    """Pure health router TestClient — no auth, no DB swap (uses configured engine)."""
    from api.health import router as health_router

    app = FastAPI()
    app.include_router(health_router)
    with TestClient(app) as client:
        yield client


def test_ready_shallow_returns_existing_shape(health_client):
    """Shallow（無 query）回傳 EXACT 既有 shape：status / db / latency_ms。

    現有 K8s/zeabur readiness probe 與監控依賴此 shape，本 PR 必不更動。
    """
    rsp = health_client.get("/health/ready")
    assert rsp.status_code == 200
    body = rsp.json()
    assert body["status"] == "ok"
    assert body["db"] == "connected"
    assert "latency_ms" in body
    # Shallow 不應暴露 components / line / supabase / db_pool
    assert "components" not in body
    assert "line" not in body
    assert "supabase" not in body
