"""驗 LineService.push_flex_to_user 邏輯（mock requests）。

覆蓋：
- disabled / no token / no line_user_id → 回傳 False，不呼叫 LINE API
- 成功 200 → 回傳 True，payload 結構正確（to / messages[0].type / altText / contents）
- LINE API 非 200 → 回傳 False
- requests.post 拋例外 → 回傳 False（不 raise）
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.line_service import LineService

_FLEX_BUBBLE = {
    "type": "bubble",
    "body": {"type": "box", "layout": "vertical", "contents": []},
}
_ALT_TEXT = "補休 3.0h 將於 2026-06-01 到期"
_LINE_USER_ID = "Uxxx_flex_test"


@pytest.fixture
def enabled_svc() -> LineService:
    """已設定 token 且 enabled=True 的 LineService（singleton-free）。"""
    svc = LineService()
    svc.configure(token="dummy_token", target_id="", enabled=True)
    return svc


@pytest.fixture
def disabled_svc() -> LineService:
    """enabled=False 的 LineService。"""
    svc = LineService()
    svc.configure(token="dummy_token", target_id="", enabled=False)
    return svc


# ── disabled / guard tests ──────────────────────────────────────────────────


def test_push_flex_disabled_returns_false(disabled_svc):
    """enabled=False → 立即回傳 False，不呼叫 LINE API。"""
    with patch("services.line_service.requests.post") as mock_post:
        result = disabled_svc.push_flex_to_user(_LINE_USER_ID, _FLEX_BUBBLE, _ALT_TEXT)
    assert result is False
    mock_post.assert_not_called()


def test_push_flex_no_token_returns_false():
    """token 為空字串 → 回傳 False。"""
    svc = LineService()
    svc.configure(token="", target_id="", enabled=True)
    with patch("services.line_service.requests.post") as mock_post:
        result = svc.push_flex_to_user(_LINE_USER_ID, _FLEX_BUBBLE, _ALT_TEXT)
    assert result is False
    mock_post.assert_not_called()


def test_push_flex_no_user_id_returns_false(enabled_svc):
    """line_user_id 為空字串 → 回傳 False。"""
    with patch("services.line_service.requests.post") as mock_post:
        result = enabled_svc.push_flex_to_user("", _FLEX_BUBBLE, _ALT_TEXT)
    assert result is False
    mock_post.assert_not_called()


# ── 成功路徑 ────────────────────────────────────────────────────────────────


@patch("services.line_service.requests.post")
def test_push_flex_calls_line_api_correctly(mock_post, enabled_svc):
    """200 回應 → 回傳 True，payload 包含正確 to / type / altText / contents。"""
    mock_post.return_value = MagicMock(status_code=200)

    result = enabled_svc.push_flex_to_user(_LINE_USER_ID, _FLEX_BUBBLE, _ALT_TEXT)

    assert result is True
    mock_post.assert_called_once()

    call_kwargs = mock_post.call_args
    # 第一個位置引數為 URL
    assert "https://api.line.me/v2/bot/message/push" in call_kwargs[0][0]

    payload = call_kwargs[1]["json"]
    assert payload["to"] == _LINE_USER_ID
    assert len(payload["messages"]) == 1

    msg = payload["messages"][0]
    assert msg["type"] == "flex"
    assert msg["altText"] == _ALT_TEXT
    assert msg["contents"] == _FLEX_BUBBLE


@patch("services.line_service.requests.post")
def test_push_flex_authorization_header(mock_post, enabled_svc):
    """requests.post 的 Authorization header 使用正確 Bearer token。"""
    mock_post.return_value = MagicMock(status_code=200)

    enabled_svc.push_flex_to_user(_LINE_USER_ID, _FLEX_BUBBLE, _ALT_TEXT)

    headers = mock_post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer dummy_token"


# ── 失敗路徑 ────────────────────────────────────────────────────────────────


@patch("services.line_service.requests.post")
def test_push_flex_non_200_returns_false(mock_post, enabled_svc):
    """LINE API 回傳非 200（例如 400）→ 回傳 False，不 raise。"""
    mock_post.return_value = MagicMock(status_code=400, text="Bad Request")

    result = enabled_svc.push_flex_to_user(_LINE_USER_ID, _FLEX_BUBBLE, _ALT_TEXT)

    assert result is False


@patch("services.line_service.requests.post")
def test_push_flex_exception_returns_false(mock_post, enabled_svc):
    """requests.post 拋例外（網路中斷等）→ 回傳 False，不 raise。"""
    mock_post.side_effect = ConnectionError("network error")

    result = enabled_svc.push_flex_to_user(_LINE_USER_ID, _FLEX_BUBBLE, _ALT_TEXT)

    assert result is False
