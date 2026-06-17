"""tests/test_health_deep.py — Ch3 deep /health/ready 行為驗證。"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def health_client():
    """Health router TestClient，deep 守衛已 override 成放行（測 deep shape 用）。"""
    from api.health import router as health_router
    from api.health import verify_deep_readiness_access

    app = FastAPI()
    app.include_router(health_router)
    # deep readiness 已加 staff-only 守衛（C29）；本 fixture 專測 deep shape，
    # 故 override 成放行。未認證攔截行為另由 test_health_deep_requires_auth 驗。
    app.dependency_overrides[verify_deep_readiness_access] = lambda: None
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


def test_ready_deep_all_green_returns_200_with_new_shape(health_client):
    """deep=1 + 全綠 → 200 + 4 component（含既有 db）+ new shape。"""
    with (
        patch(
            "api.health._check_line",
            return_value={"ok": True, "breaker": "closed", "consecutive_failures": 0},
        ),
        patch(
            "api.health._check_supabase",
            return_value={"ok": True, "breaker": "closed", "pending_uploads": 0},
        ),
        patch(
            "api.health._check_db_pool",
            return_value={"ok": True, "used": 1, "size": 5, "utilization": 0.2},
        ),
    ):
        rsp = health_client.get("/health/ready?deep=1")

    assert rsp.status_code == 200
    body = rsp.json()
    assert body["status"] == "ok"
    # Deep shape：components dict 含 4 key
    assert set(body["components"].keys()) == {"db", "line", "supabase", "db_pool"}
    assert all(c.get("ok") for c in body["components"].values())


def test_ready_deep_line_breaker_open_returns_503(health_client):
    """deep=1 + LINE breaker open → 503，body 含 line.breaker='open'."""
    with (
        patch(
            "api.health._check_line",
            return_value={"ok": False, "breaker": "open", "consecutive_failures": 6},
        ),
        patch(
            "api.health._check_supabase",
            return_value={"ok": True, "breaker": "closed", "pending_uploads": 0},
        ),
        patch(
            "api.health._check_db_pool",
            return_value={"ok": True, "used": 1, "size": 5, "utilization": 0.2},
        ),
    ):
        rsp = health_client.get("/health/ready?deep=1")

    assert rsp.status_code == 503
    body = rsp.json()
    assert body["status"] == "degraded"
    assert body["components"]["line"]["breaker"] == "open"
    assert body["components"]["line"]["ok"] is False


def test_ready_deep_supabase_pending_overflow_returns_503(health_client):
    """deep=1 + pending_uploads > 50 (積壓警戒) → 503。"""
    with (
        patch(
            "api.health._check_line",
            return_value={"ok": True, "breaker": "closed", "consecutive_failures": 0},
        ),
        patch(
            "api.health._check_supabase",
            return_value={"ok": False, "breaker": "closed", "pending_uploads": 75},
        ),
        patch(
            "api.health._check_db_pool",
            return_value={"ok": True, "used": 1, "size": 5, "utilization": 0.2},
        ),
    ):
        rsp = health_client.get("/health/ready?deep=1")

    assert rsp.status_code == 503
    body = rsp.json()
    assert body["components"]["supabase"]["pending_uploads"] == 75


def test_ready_shallow_unaffected_by_deep_component_failures(health_client):
    """shallow（無 query）即便 LINE/Supabase 都 open 仍回 200 + 既有 shape。

    保證 K8s readiness probe 不會因 LINE/Supabase 偶發 outage 而把整個 pod 砍掉。
    """
    with (
        patch(
            "api.health._check_line",
            return_value={"ok": False, "breaker": "open"},
        ),
        patch(
            "api.health._check_supabase",
            return_value={"ok": False, "breaker": "open"},
        ),
    ):
        rsp = health_client.get("/health/ready")  # 無 deep

    assert rsp.status_code == 200
    body = rsp.json()
    assert body["status"] == "ok"
    assert body["db"] == "connected"
    assert "components" not in body


def test_deep_readiness_blocks_unauthenticated_and_hides_internals():
    """C29：未認證 ?deep=1 不得回 db_pool/breaker/parent_portal.detail 內部明細。

    shallow（無 deep）維持公開；deep 為 staff-only。未認證打 deep → 403，
    且 body 不含 components / db_pool / parent_portal。
    """
    from api.health import router as health_router

    app = FastAPI()
    app.include_router(health_router)  # 不 override 守衛 → 走真實認證
    with TestClient(app) as client:
        # shallow 仍公開
        shallow = client.get("/health/ready")
        assert shallow.status_code == 200

        # deep 未認證 → 401/403，無內部明細外洩
        deep = client.get("/health/ready?deep=1")
        assert deep.status_code in (401, 403)
        body = deep.json()
        assert "components" not in body
        assert "db_pool" not in str(body)
        assert "parent_portal" not in body
