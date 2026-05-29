"""LINE webhook rate-limit 接入測試。

Memory limiter backend（RATE_LIMIT_BACKEND=memory）下，超過 N 次同 channel
key 後第 N+1 次拿 429。為避免測 1000 次，monkeypatch 模組 singleton 換成
低 threshold。
"""

import base64
import hashlib
import hmac
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_line_webhook_rate_limit_exceeds_429(monkeypatch):
    """前 3 次 pass，第 4 次超 (limiter max=3) 應 429。"""
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")

    from utils.rate_limit import create_limiter

    test_limiter = create_limiter(
        max_calls=3,
        window_seconds=60,
        name="line_webhook_test",
        error_detail="LINE webhook rate-limit exceeded (test)",
    )
    import api.line_webhook as lw

    monkeypatch.setattr(lw, "_LINE_WEBHOOK_LIMITER", test_limiter)

    class FakeService:
        _channel_secret = "test_secret"

    monkeypatch.setattr(lw, "_line_service", FakeService())

    app = FastAPI()
    app.include_router(lw.router)
    client = TestClient(app)

    body = json.dumps({"events": []}).encode()
    sig = _signature("test_secret", body)

    for i in range(3):
        r = client.post(
            "/api/line/webhook",
            content=body,
            headers={"X-Line-Signature": sig},
        )
        assert r.status_code == 200, f"第 {i + 1} 次應 pass，得 {r.status_code}"

    r = client.post(
        "/api/line/webhook",
        content=body,
        headers={"X-Line-Signature": sig},
    )
    assert r.status_code == 429
