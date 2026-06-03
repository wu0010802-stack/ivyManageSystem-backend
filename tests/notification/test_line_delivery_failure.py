"""LINE push HTTP 失敗（4xx/5xx/429）在 dispatch 情境須 raise，不可靜默誤記送達。

稽核 2026-06-03 P1#4：line_service push 對任何非 200 回應經 _record_line_response
回 False 且不 raise；LineAdapter.send 忽略回傳 → dispatch._fan_out 誤判 succeeded、
retry_scheduler 誤判成功。淨效果：每個 HTTP 層失敗（封鎖 403 / 額度 429 / 無效 400 /
5xx）都被當成已送達且不重試，整套 retry/circuit-breaker 只能攔 ConnectionError/Timeout。

修法：dispatch_delivery_strict() 區塊內 _record_line_response 失敗即 raise
LineDeliveryError，讓既有 retry（3 次 backoff）+ dispatch channels_failed 接手；
webhook reply 等區塊外 caller 維持 bool 回傳不受影響。
"""

import pytest

import services.line_service as line_service_mod
from services.line_service import (
    LineDeliveryError,
    LineService,
    _record_line_response,
    dispatch_delivery_strict,
)
from utils.circuit_breaker import LINE_BREAKER
from services.notification._channels.line import LineAdapter
from services.notification.dispatch import PendingEvent
from services.notification.renderers import Rendered


class _FakeResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def test_record_line_response_returns_bool_outside_dispatch_scope():
    """區塊外（webhook reply 等）維持既有 bool 回傳語意，不 raise。"""
    assert _record_line_response(_FakeResp(200), context="t") is True
    assert _record_line_response(_FakeResp(429), context="t") is False
    assert _record_line_response(_FakeResp(403), context="t") is False


def test_record_line_response_raises_in_dispatch_strict_scope():
    """dispatch 情境下任何非 200 失敗皆 raise；200 不 raise；離開區塊恢復 bool。"""
    with dispatch_delivery_strict():
        assert _record_line_response(_FakeResp(200), context="t") is True
        for bad in (400, 401, 403, 404, 429, 500, 503):
            with pytest.raises(LineDeliveryError):
                _record_line_response(_FakeResp(bad), context="t")
    assert _record_line_response(_FakeResp(429), context="t") is False


def test_line_adapter_send_raises_on_http_failure(monkeypatch):
    """端到端：adapter.send 對真實 push 的 429 回應 raise → dispatch 會記 failed + 排重試。"""
    LINE_BREAKER.reset()
    ls = LineService()
    ls.configure(token="tok", target_id="Cgroup", enabled=True)
    monkeypatch.setattr(
        line_service_mod.requests,
        "post",
        lambda *a, **k: _FakeResp(429, "rate limited"),
    )
    adapter = LineAdapter(ls)
    evt = PendingEvent(
        event_type="dismissal.created",
        recipient_user_id=None,
        context={"student_name": "小明", "classroom_name": "向日葵班", "note": "x"},
        sender_id=None,
        source_entity_type="dismissal_call",
        source_entity_id=1,
        channels=("line",),
        line_group_id="",
    )
    with pytest.raises(LineDeliveryError):
        adapter.send(evt, Rendered(title="t", body="b", deep_link=None), log_id=1)
