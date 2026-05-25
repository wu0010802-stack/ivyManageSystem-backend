"""LINE adapter 測試：thin dispatch 表 + fallback push_text。"""

import pytest
from unittest.mock import MagicMock

from services.notification._channels.line import LineAdapter, LINE_HANDLERS
from services.notification.dispatch import PendingEvent
from services.notification.renderers import Rendered


def _evt(event_type, **kwargs):
    return PendingEvent(
        event_type=event_type,
        recipient_user_id=kwargs.get("recipient_user_id", 1),
        context=kwargs.get("context", {}),
        sender_id=None,
        source_entity_type=None,
        source_entity_id=None,
        channels=("line",),
    )


def test_line_adapter_fallback_push_text_for_unmapped_event_type():
    """Phase 1：LINE_HANDLERS 為空（全部走 fallback push_text_to_user）。"""
    fake_ls = MagicMock()
    adapter = LineAdapter(fake_ls)
    # 用 str recipient 模擬 Phase 2 resolve 後的狀態
    adapter.send(
        _evt("leave.approved", recipient_user_id="Uxxxxxxxxxx"),
        Rendered(title="標題", body="內文", deep_link="/x"),
        log_id=1,
    )
    fake_ls.push_text_to_user.assert_called_once()
    args = fake_ls.push_text_to_user.call_args[0]
    assert args[0] == "Uxxxxxxxxxx"
    assert "標題" in args[1]
    assert "內文" in args[1]


def test_line_adapter_skips_when_recipient_is_int_with_warning(caplog):
    """Phase 1 fallback：未 resolve 為 LINE user_id 的 int 應 skip + warning。"""
    fake_ls = MagicMock()
    adapter = LineAdapter(fake_ls)
    with caplog.at_level("WARNING"):
        adapter.send(
            _evt("leave.approved", recipient_user_id=42),
            Rendered(title="t", body="b", deep_link=None),
            log_id=1,
        )
    fake_ls.push_text_to_user.assert_not_called()
    assert any("recipient_user_id" in r.message for r in caplog.records)


def test_line_adapter_calls_handler_when_registered():
    """若 LINE_HANDLERS 有對映，應 dispatch 給 handler。"""
    fake_ls = MagicMock()
    handler_called = []

    def my_handler(ls, evt, rendered):
        handler_called.append((ls, evt.event_type, rendered.title))

    LINE_HANDLERS["leave.approved"] = my_handler
    try:
        adapter = LineAdapter(fake_ls)
        adapter.send(
            _evt("leave.approved"),
            Rendered(title="t", body="b", deep_link=None),
            log_id=1,
        )
        assert len(handler_called) == 1
        assert handler_called[0] == (fake_ls, "leave.approved", "t")
        fake_ls.push_text_to_user.assert_not_called()
    finally:
        del LINE_HANDLERS["leave.approved"]


def test_line_handlers_is_empty_at_phase_1():
    """Phase 1 不註冊任何 handler，全部走 fallback。Phase 2 PR-A 開始填入。"""
    assert LINE_HANDLERS == {}
