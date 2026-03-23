"""
LINE Webhook endpoint 單元測試
"""

import base64
import hashlib
import hmac
import json
import pytest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from fastapi import FastAPI

# ──────────────────────────────────────────────────────────────────────────────
# 輔助函式
# ──────────────────────────────────────────────────────────────────────────────

def _make_sig(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _make_app(line_svc=None):
    """建立含 line_webhook router 的測試 app"""
    from api.line_webhook import router, init_webhook_service
    app = FastAPI()
    init_webhook_service(line_svc)
    app.include_router(router)
    return app


# ──────────────────────────────────────────────────────────────────────────────
# 簽名驗證
# ──────────────────────────────────────────────────────────────────────────────

class TestSignatureVerification:
    def test_no_secret_returns_503(self):
        """尚未設定 channel_secret 時回傳 503"""
        svc = MagicMock()
        svc._channel_secret = None
        app = _make_app(svc)
        client = TestClient(app, raise_server_exceptions=False)
        body = b'{"events":[]}'
        resp = client.post("/api/line/webhook", content=body)
        assert resp.status_code == 503

    def test_invalid_sig_returns_400(self):
        """簽名不符時回傳 400"""
        svc = MagicMock()
        svc._channel_secret = "my-secret"
        app = _make_app(svc)
        client = TestClient(app, raise_server_exceptions=False)
        body = b'{"events":[]}'
        resp = client.post(
            "/api/line/webhook",
            content=body,
            headers={"X-Line-Signature": "invalidsig=="},
        )
        assert resp.status_code == 400

    def test_valid_sig_passes(self):
        """簽名正確時回傳 200"""
        secret = "valid-secret"
        svc = MagicMock()
        svc._channel_secret = secret
        app = _make_app(svc)
        client = TestClient(app, raise_server_exceptions=False)
        body = json.dumps({"events": []}).encode()
        sig = _make_sig(secret, body)
        resp = client.post(
            "/api/line/webhook",
            content=body,
            headers={"X-Line-Signature": sig},
        )
        assert resp.status_code == 200


# ──────────────────────────────────────────────────────────────────────────────
# Webhook 事件分發
# ──────────────────────────────────────────────────────────────────────────────

class TestWebhookEvents:
    SECRET = "test-secret"

    def _post(self, app, events):
        client = TestClient(app, raise_server_exceptions=False)
        body = json.dumps({"events": events}).encode()
        sig = _make_sig(self.SECRET, body)
        return client.post(
            "/api/line/webhook",
            content=body,
            headers={"X-Line-Signature": sig},
        )

    def _make_svc(self):
        svc = MagicMock()
        svc._channel_secret = self.SECRET
        return svc

    def test_follow_event_replies_user_id(self):
        """follow 事件時 Bot 回覆用戶 ID"""
        svc = self._make_svc()
        app = _make_app(svc)

        events = [{
            "type": "follow",
            "replyToken": "reply-token-abc",
            "source": {"type": "user", "userId": "Uabcd1234"},
        }]
        resp = self._post(app, events)
        assert resp.status_code == 200
        # 確認呼叫了 _reply 且訊息含 User ID
        svc._reply.assert_called_once()
        call_args = svc._reply.call_args
        assert "Uabcd1234" in call_args[0][1]

    def test_text_event_calls_handle_webhook_message(self):
        """text message 事件呼叫 handle_webhook_message"""
        svc = self._make_svc()
        app = _make_app(svc)

        events = [{
            "type": "message",
            "replyToken": "reply-token-xyz",
            "source": {"type": "user", "userId": "Utest1111"},
            "message": {"type": "text", "text": "我的薪資"},
        }]
        with patch("api.line_webhook.get_session") as mock_session:
            mock_session.return_value.__enter__ = MagicMock()
            mock_session.return_value.close = MagicMock()
            mock_sess = MagicMock()
            mock_session.return_value = mock_sess
            resp = self._post(app, events)

        assert resp.status_code == 200
        svc.handle_webhook_message.assert_called_once()
        call_args = svc.handle_webhook_message.call_args[0]
        assert call_args[0] == "Utest1111"
        assert call_args[1] == "我的薪資"

    def test_unknown_event_returns_200(self):
        """未知事件類型不報錯，回傳 200"""
        svc = self._make_svc()
        app = _make_app(svc)

        events = [{"type": "postback", "source": {"userId": "Utest"}, "postback": {"data": "action=buy"}}]
        resp = self._post(app, events)
        assert resp.status_code == 200
