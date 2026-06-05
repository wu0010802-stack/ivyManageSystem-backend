"""tests/test_ws_origin_middleware.py — Finding J：WS Origin 防護（CSWSH）。

純 ASGI middleware 單元測試：以假 scope/receive/send 驅動，斷言
- 不允許的 Origin → 拒絕 handshake（websocket.close），downstream app 不被呼叫
- 允許的 Origin → 放行
- Origin 缺失（非瀏覽器）→ 放行
- http scope → 直接 pass through
"""

import os
import sys
import asyncio

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import middleware.ws_origin as ws_origin_mod
from middleware.ws_origin import WSOriginCheckMiddleware, WS_CLOSE_ORIGIN_FORBIDDEN


def _run(scope, *, allowed):
    """跑 middleware，回傳 (downstream_called, sent_messages)。"""
    called = {"v": False}

    async def app(scope, receive, send):
        called["v"] = True

    async def receive():
        return {"type": "websocket.connect"}

    sent = []

    async def send(msg):
        sent.append(msg)

    mw = WSOriginCheckMiddleware(app)
    # patch allowlist 來源，避免依賴 env/config
    orig = ws_origin_mod._get_allowed_origins
    ws_origin_mod._get_allowed_origins = lambda: allowed
    try:
        asyncio.run(mw(scope, receive, send))
    finally:
        ws_origin_mod._get_allowed_origins = orig
    return called["v"], sent


_ALLOW = ["https://app.example.com"]


def test_rejects_disallowed_origin():
    scope = {
        "type": "websocket",
        "path": "/api/ws/admin/dismissal-calls",
        "headers": [(b"origin", b"https://evil.example.com")],
    }
    called, sent = _run(scope, allowed=_ALLOW)
    assert called is False, "惡意 Origin 不應放行到 downstream WS handler"
    assert any(
        m.get("type") == "websocket.close"
        and m.get("code") == WS_CLOSE_ORIGIN_FORBIDDEN
        for m in sent
    )


def test_allows_allowlisted_origin():
    scope = {
        "type": "websocket",
        "path": "/api/ws/admin/dismissal-calls",
        "headers": [(b"origin", b"https://app.example.com")],
    }
    called, sent = _run(scope, allowed=_ALLOW)
    assert called is True
    assert sent == []


def test_allows_missing_origin_non_browser():
    scope = {"type": "websocket", "path": "/api/ws/x", "headers": []}
    called, sent = _run(scope, allowed=_ALLOW)
    assert called is True


def test_passes_through_http_scope():
    scope = {
        "type": "http",
        "path": "/api/employees",
        "headers": [(b"origin", b"https://evil.example.com")],
    }
    called, sent = _run(scope, allowed=_ALLOW)
    assert called is True, "http scope 不歸本 middleware 管，須 pass through"
