"""POST /api/internal/uptime-webhook 行為驗證。

UptimeRobot 公開可打（query param token 驗證）；alert payload 轉中文 →
push 到 OPS_ALERT_LINE_GROUP_ID。

Cases：
- invalid token → 401
- valid token + alertType=1 (down) → 200 + LineService.push_text_to_group 被呼叫且訊息含「宕機」
- valid token 但 OPS_ALERT_LINE_GROUP_ID 未設 → 200 + status=skipped
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.internal.uptime_webhook import (
    init_uptime_webhook_service,
    reset_for_tests as reset_uptime,
    router as uptime_router,
)


@pytest.fixture
def webhook_client(monkeypatch):
    """FastAPI client with uptime-webhook router + injected mock LineService。"""
    monkeypatch.setenv("UPTIME_ROBOT_WEBHOOK_TOKEN", "secret-xyz")
    monkeypatch.setenv("OPS_ALERT_LINE_GROUP_ID", "Cgroup123")
    # reset_for_tests is autouse — settings re-read on next get_settings()
    mock_line = MagicMock()
    mock_line.push_text_to_group.return_value = True
    init_uptime_webhook_service(mock_line)
    app = FastAPI()
    app.include_router(uptime_router)
    with TestClient(app) as client:
        yield client, mock_line
    reset_uptime()


def test_uptime_webhook_invalid_token_returns_401(webhook_client):
    client, _ = webhook_client
    r = client.post("/api/internal/uptime-webhook?token=wrong", json={})
    assert r.status_code == 401


def test_uptime_webhook_alert_type_1_pushes_down_message(webhook_client):
    client, mock_line = webhook_client
    payload = {
        "monitorFriendlyName": "Ivy /health/ready",
        "alertType": "1",  # 1 = down
        "alertDetails": "Connection timeout",
    }
    r = client.post("/api/internal/uptime-webhook?token=secret-xyz", json=payload)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    mock_line.push_text_to_group.assert_called_once()
    args, _ = mock_line.push_text_to_group.call_args
    group_id, msg = args
    assert group_id == "Cgroup123"
    assert "Ivy /health/ready" in msg
    assert "宕機" in msg
    assert "Connection timeout" in msg


def test_uptime_webhook_skip_when_group_id_missing(monkeypatch):
    """OPS_ALERT_LINE_GROUP_ID 未設 → 不 push，回 status=skipped。"""
    monkeypatch.setenv("UPTIME_ROBOT_WEBHOOK_TOKEN", "secret-xyz")
    monkeypatch.delenv("OPS_ALERT_LINE_GROUP_ID", raising=False)
    mock_line = MagicMock()
    init_uptime_webhook_service(mock_line)
    app = FastAPI()
    app.include_router(uptime_router)
    with TestClient(app) as client:
        r = client.post(
            "/api/internal/uptime-webhook?token=secret-xyz",
            json={"monitorFriendlyName": "X", "alertType": "1", "alertDetails": "y"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"
    mock_line.push_text_to_group.assert_not_called()
    reset_uptime()
