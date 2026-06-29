"""dismissal_ws 兩端點 handshake 後須週期性重驗 token（傳 verify= 給 run_ws_connection）。

QA loop（2026-06-29）：sibling contact_book_ws 已傳 verify=lambda: _token_still_valid(ws)，
但 dismissal_ws.portal_dismissal_ws / admin_dismissal_ws 呼叫 _run_connection 時未帶 verify
→ handshake 後永不重驗 token，登出（token_version bump + jti 入黑名單）/ 停用後連線仍存活整個
生命週期（遠超 ~15min token），admin channel 持續推全校接送 PII。這正是 P2-8（WS_REVERIFY）
設計要關閉的窗口，唯獨在最敏感的全校接送通道留著沒接。

修法：兩端點都傳 verify=lambda: _token_still_valid(ws)（內部重新 verify_ws_token）。
"""

import os
import sys
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import WebSocketDisconnect

import api.dismissal_ws as dws
from utils import ws_connection_limiter


@pytest.fixture(autouse=True)
def _reset_limiter():
    ws_connection_limiter.reset_for_tests()
    yield
    ws_connection_limiter.reset_for_tests()


def _make_ws():
    ws = MagicMock()
    ws.cookies = {"access_token": "dummy"}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_text = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect(code=1000))
    return ws


def _run_capture_verify(endpoint, payload, classroom_ids=None):
    """跑 endpoint coroutine，攔截傳給 _run_connection 的 verify 回調。"""
    ws = _make_ws()
    backend = MagicMock()
    captured = {}

    async def _fake_run_connection(conn_ws, cleanup=None, verify=None, **kwargs):
        captured["verify"] = verify

    with (
        patch.object(dws, "verify_ws_token", return_value=payload),
        patch.object(dws, "get_broadcast", return_value=backend),
        patch.object(
            dws, "_get_teacher_classroom_ids", return_value=(classroom_ids or [])
        ),
        patch.object(dws, "_run_connection", side_effect=_fake_run_connection),
    ):
        asyncio.run(endpoint(ws))
    return captured


_ADMIN_PAYLOAD = {
    "user_id": 1,
    "role": "admin",
    "employee_id": None,
    "permission_names": ["*"],
}


def test_portal_dismissal_ws_passes_verify_callback():
    captured = _run_capture_verify(dws.portal_dismissal_ws, _ADMIN_PAYLOAD)
    assert captured.get("verify") is not None, "portal 接送 WS 須傳 verify 重驗回調"
    assert callable(captured["verify"])


def test_admin_dismissal_ws_passes_verify_callback():
    captured = _run_capture_verify(dws.admin_dismissal_ws, _ADMIN_PAYLOAD)
    assert captured.get("verify") is not None, "admin 接送 WS 須傳 verify 重驗回調"
    assert callable(captured["verify"])


def test_verify_callback_revalidates_token():
    """攔到的 verify 回調被呼叫時應重新 verify_ws_token：撤銷→False、有效→True。"""
    captured = _run_capture_verify(dws.portal_dismissal_ws, _ADMIN_PAYLOAD)
    verify = captured["verify"]
    with patch.object(dws, "verify_ws_token", side_effect=Exception("revoked")):
        assert verify() is False
    with patch.object(dws, "verify_ws_token", return_value=_ADMIN_PAYLOAD):
        assert verify() is True
