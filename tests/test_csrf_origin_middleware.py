"""Spec B: CSRFOriginCheckMiddleware 6 個 pytest。

用 minimal FastAPI app + middleware，避免 main.py 整體啟動 cost。
monkeypatch settings.network.cors_origins 設 fixed 白名單。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.csrf_origin import CSRFOriginCheckMiddleware


@pytest.fixture
def app_with_csrf(monkeypatch):
    """Minimal app: middleware + 兩條 dummy route + bypass route。"""
    from config import settings

    monkeypatch.setattr(
        settings.network,
        "cors_origins",
        ["http://allowed.example.com", "https://allowed.example.com"],
    )

    app = FastAPI()
    app.add_middleware(CSRFOriginCheckMiddleware)

    @app.get("/api/safe")
    def safe_get():
        return {"ok": True}

    @app.post("/api/unsafe")
    def unsafe_post():
        return {"ok": True}

    @app.post("/api/line/webhook")
    def line_webhook():
        return {"ok": True}

    @app.post("/api/activity/public/register")
    def public_register():
        return {"ok": True}

    @app.post("/api/attendance/kiosk/preview")
    def kiosk_preview():
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=True)


def test_safe_methods_pass_without_origin(app_with_csrf):
    """GET/HEAD/OPTIONS skip CSRF check 即使無 Origin/Referer 也 200。"""
    res = app_with_csrf.get("/api/safe")
    assert res.status_code == 200


def test_post_without_origin_returns_403(app_with_csrf):
    """POST 無 Origin/Referer → 403 + 含 missing origin/referer detail。"""
    # 明確帶空 Origin/Referer 確保 TestClient 不自動注入任何值
    res = app_with_csrf.post(
        "/api/unsafe",
        headers={"Origin": "", "Referer": ""},
    )
    assert res.status_code == 403
    assert "missing origin/referer" in res.json()["detail"]


def test_post_with_allowed_origin_passes(app_with_csrf):
    """POST + Origin in cors_origins → 200。"""
    res = app_with_csrf.post(
        "/api/unsafe", headers={"Origin": "http://allowed.example.com"}
    )
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_post_with_disallowed_origin_returns_403(app_with_csrf):
    """POST + Origin 不在 cors_origins → 403 + 含 origin not allowed detail。"""
    res = app_with_csrf.post(
        "/api/unsafe", headers={"Origin": "http://evil.example.com"}
    )
    assert res.status_code == 403
    assert "origin not allowed" in res.json()["detail"]


def test_post_with_referer_fallback_passes(app_with_csrf):
    """POST 缺 Origin、Referer 在白名單 → 200。"""
    res = app_with_csrf.post(
        "/api/unsafe",
        headers={"Referer": "http://allowed.example.com/some/page"},
    )
    assert res.status_code == 200


def test_bypass_paths_skip_csrf(app_with_csrf):
    """POST /api/line/webhook + /api/activity/public/* 無 Origin → 200（path bypass）。"""
    res1 = app_with_csrf.post("/api/line/webhook")
    res2 = app_with_csrf.post("/api/activity/public/register")
    assert res1.status_code == 200
    assert res2.status_code == 200


def test_kiosk_path_bypasses_csrf(app_with_csrf):
    """POST /api/attendance/kiosk/* 不帶 Origin → 通過 CSRF 層（200）。

    kiosk 裝置無 cookie/session，CSRF 攻擊面不存在；
    已有 IP 白名單 + PIN 雙重保護，豁免合理。
    """
    res = app_with_csrf.post("/api/attendance/kiosk/preview")
    # 200 = CSRF 中介層放行（無 Origin 仍通過）
    assert res.status_code == 200
