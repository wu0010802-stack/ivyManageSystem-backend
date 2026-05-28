"""KillSwitchMiddleware 單元測試。

env-driven 維護/唯讀 503 短路，與 main.py 無關（純 starlette middleware）。
整合層測試見 tests/test_main_kill_switch_integration.py。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Bypass paths 規格（與 middleware 內 BYPASS_PATHS 對齊）
EXPECTED_BYPASS_PATHS = (
    "/health/live",
    "/health/ready",
    "/health/schedulers",
    "/api/internal/uptime-webhook",
    "/auth/login",
    "/auth/refresh",
)


def _make_app(maintenance=False, read_only=False, message="維護中", monkeypatch=None):
    """組裝 FastAPI app + KillSwitchMiddleware + 6 個 bypass + 2 個 non-bypass 端點。

    monkeypatch 必填：透過 setenv + reset_for_tests 設定 ops 旗標。
    """
    if maintenance:
        monkeypatch.setenv("MAINTENANCE_MODE", "1")
    else:
        monkeypatch.delenv("MAINTENANCE_MODE", raising=False)
    if read_only:
        monkeypatch.setenv("READ_ONLY_MODE", "1")
    else:
        monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    monkeypatch.setenv("MAINTENANCE_MESSAGE", message)

    from config import reset_for_tests

    reset_for_tests()

    from utils.kill_switch import KillSwitchMiddleware

    app = FastAPI()
    app.add_middleware(KillSwitchMiddleware)

    @app.get("/test")
    async def t_get():
        return {"ok": True}

    @app.post("/test")
    async def t_post():
        return {"ok": True}

    @app.patch("/test")
    async def t_patch():
        return {"ok": True}

    @app.put("/test")
    async def t_put():
        return {"ok": True}

    @app.delete("/test")
    async def t_delete():
        return {"ok": True}

    # 註冊全部 6 條 bypass 路徑（covering test）
    for path in EXPECTED_BYPASS_PATHS:

        async def _bypass_get(_path=path):
            return {"bypass": _path}

        async def _bypass_post(_path=path):
            return {"bypass": _path, "method": "POST"}

        app.add_api_route(path, _bypass_get, methods=["GET"])
        app.add_api_route(path, _bypass_post, methods=["POST"])

    return app


def test_normal_passes_through(monkeypatch):
    app = _make_app(monkeypatch=monkeypatch)
    client = TestClient(app)
    r = client.get("/test")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_maintenance_blocks_get(monkeypatch):
    app = _make_app(maintenance=True, monkeypatch=monkeypatch)
    r = TestClient(app).get("/test")
    assert r.status_code == 503
    payload = r.json()
    assert payload["detail"]["code"] == "MAINTENANCE_MODE"
    assert payload["detail"]["message"] == "維護中"
    assert payload["detail"]["retry_after"] == 300
    assert r.headers["retry-after"] == "300"


def test_maintenance_blocks_post(monkeypatch):
    app = _make_app(maintenance=True, monkeypatch=monkeypatch)
    r = TestClient(app).post("/test", json={})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "MAINTENANCE_MODE"


def test_maintenance_uses_custom_message(monkeypatch):
    app = _make_app(maintenance=True, message="升級至 v2", monkeypatch=monkeypatch)
    r = TestClient(app).get("/test")
    assert r.json()["detail"]["message"] == "升級至 v2"


def test_read_only_blocks_post(monkeypatch):
    app = _make_app(read_only=True, monkeypatch=monkeypatch)
    r = TestClient(app).post("/test", json={})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "READ_ONLY_MODE"
    assert r.headers["retry-after"] == "300"


def test_read_only_blocks_patch(monkeypatch):
    app = _make_app(read_only=True, monkeypatch=monkeypatch)
    r = TestClient(app).patch("/test", json={})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "READ_ONLY_MODE"


def test_read_only_blocks_put(monkeypatch):
    app = _make_app(read_only=True, monkeypatch=monkeypatch)
    r = TestClient(app).put("/test", json={})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "READ_ONLY_MODE"


def test_read_only_blocks_delete(monkeypatch):
    app = _make_app(read_only=True, monkeypatch=monkeypatch)
    r = TestClient(app).delete("/test")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "READ_ONLY_MODE"


def test_read_only_allows_get(monkeypatch):
    app = _make_app(read_only=True, monkeypatch=monkeypatch)
    r = TestClient(app).get("/test")
    assert r.status_code == 200


def test_read_only_allows_head(monkeypatch):
    app = _make_app(read_only=True, monkeypatch=monkeypatch)
    r = TestClient(app).head("/test")
    # FastAPI 自動處理 HEAD：starlette 會把 GET 路由認為支援 HEAD
    assert r.status_code in (200, 405)
    # 重點：不是 503（read_only 不該擋唯讀操作）
    assert r.status_code != 503


def test_read_only_allows_options(monkeypatch):
    app = _make_app(read_only=True, monkeypatch=monkeypatch)
    r = TestClient(app).options("/test")
    # 至少不能是 503（read_only 不該擋 CORS preflight）
    assert r.status_code != 503


def test_maintenance_takes_priority_over_read_only(monkeypatch):
    """同時開兩 flag 時 MAINTENANCE 先擋（含 GET 也擋）。"""
    app = _make_app(maintenance=True, read_only=True, monkeypatch=monkeypatch)
    r = TestClient(app).get("/test")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "MAINTENANCE_MODE"


@pytest.mark.parametrize("path", EXPECTED_BYPASS_PATHS)
def test_maintenance_bypasses_path(monkeypatch, path):
    app = _make_app(maintenance=True, monkeypatch=monkeypatch)
    r = TestClient(app).get(path)
    assert r.status_code == 200, f"bypass {path} 在 maintenance 仍被擋"
    assert r.json()["bypass"] == path


@pytest.mark.parametrize("path", EXPECTED_BYPASS_PATHS)
def test_read_only_bypasses_path_post(monkeypatch, path):
    """auth/login + uptime-webhook 是 POST，read_only 模式也得讓 POST 通過。"""
    app = _make_app(read_only=True, monkeypatch=monkeypatch)
    r = TestClient(app).post(path)
    assert r.status_code == 200, f"bypass {path} 在 read_only 仍被擋"


def test_envelope_shape_complete(monkeypatch):
    """確認 503 envelope 是 {detail: {message, code, retry_after}}，與 spec §4 對齊。"""
    app = _make_app(maintenance=True, monkeypatch=monkeypatch)
    r = TestClient(app).get("/test")
    detail = r.json()["detail"]
    assert set(detail.keys()) == {"message", "code", "retry_after"}, detail
    assert isinstance(detail["message"], str)
    assert isinstance(detail["code"], str)
    assert isinstance(detail["retry_after"], int)
