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


UNREGISTERED_EVENT = "dismissal.created"
# dismissal.created Section 2 (group_id mode) 才接管；本 Phase 4 Section 1 後
# 仍走 fallback push_text。用此 event 驗 LineAdapter fallback path。


def test_line_adapter_fallback_push_text_for_unmapped_event_type():
    """Phase 4 Section 1 後 LINE_HANDLERS 覆蓋 22 個 event；剩 dismissal.created
    仍未註冊（hybrid path），其發送會走 fallback push_text_to_user。"""
    fake_ls = MagicMock()
    adapter = LineAdapter(fake_ls)
    adapter.send(
        _evt(UNREGISTERED_EVENT, recipient_user_id="Uxxxxxxxxxx"),
        Rendered(title="標題", body="內文", deep_link="/x"),
        log_id=1,
    )
    fake_ls.push_text_to_user.assert_called_once()
    args = fake_ls.push_text_to_user.call_args[0]
    assert args[0] == "Uxxxxxxxxxx"
    assert "標題" in args[1]
    assert "內文" in args[1]


def test_line_adapter_raises_when_recipient_is_int():
    """LineAdapter 收到非 str recipient_user_id 應 raise ValueError（走 fallback path）。

    用未註冊 event_type（dismissal.created）觸發 fallback 才會撞到 isinstance 檢查；
    註冊 event 走 handler 不會 reach 該 check。
    """
    fake_ls = MagicMock()
    adapter = LineAdapter(fake_ls)
    with pytest.raises(ValueError, match="_resolve_line_user_id"):
        adapter.send(
            _evt(UNREGISTERED_EVENT, recipient_user_id=42),
            Rendered(title="t", body="b", deep_link=None),
            log_id=1,
        )
    fake_ls.push_text_to_user.assert_not_called()


def test_line_adapter_calls_handler_when_registered():
    """若 LINE_HANDLERS 有對映，應 dispatch 給 handler。

    用真 leave.approved（Section 1 已註冊）驗證；mock 蓋過真 handler 觀察 call。
    """
    fake_ls = MagicMock()
    handler_called = []
    orig = LINE_HANDLERS["leave.approved"]

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
        LINE_HANDLERS["leave.approved"] = orig


def test_line_handlers_covers_22_events():
    """Phase 4 Section 1 後 LINE_HANDLERS 覆蓋 22 個 event_type（23 個減 dismissal.created）。
    dismissal.created 留給 Section 2 group_id mode 處理；其他 event 全部走專屬
    handler 取代 fallback push_text，恢復 Flex/quick-reply 互動 UI。"""
    assert UNREGISTERED_EVENT not in LINE_HANDLERS
    # 員工域 11 + 家長域 7 + 才藝家長 3 + Growth Report 1 = 22
    assert len(LINE_HANDLERS) == 22


def test_parent_message_handler_uses_quick_reply_when_thread_id_present():
    """parent.message_received 帶 thread_id → 推送含 quick-reply postback。"""
    fake_ls = MagicMock()
    adapter = LineAdapter(fake_ls)
    adapter.send(
        _evt(
            "parent.message_received",
            recipient_user_id="Uxxxxx",
            context={
                "teacher_name": "王老師",
                "student_name": "小明",
                "body_preview": "今天有點不舒服",
                "thread_id": 42,
            },
        ),
        Rendered(title="t", body="b", deep_link=None),
        log_id=1,
    )
    fake_ls._push_to_user_with_quick_reply.assert_called_once()
    args = fake_ls._push_to_user_with_quick_reply.call_args[0]
    assert args[0] == "Uxxxxx"
    assert "王老師" in args[1]
    quick_reply = args[2]
    assert quick_reply["items"][0]["action"]["data"] == "thread_id=42"
    fake_ls._push_to_user.assert_not_called()


def test_parent_message_handler_no_quick_reply_when_thread_id_absent():
    """parent.message_received 無 thread_id → 走純 push_to_user 無 quick-reply。"""
    fake_ls = MagicMock()
    adapter = LineAdapter(fake_ls)
    adapter.send(
        _evt(
            "parent.message_received",
            recipient_user_id="Uxxxxx",
            context={"teacher_name": "王老師", "body_preview": "hello"},
        ),
        Rendered(title="t", body="b", deep_link=None),
        log_id=1,
    )
    fake_ls._push_to_user.assert_called_once()
    fake_ls._push_to_user_with_quick_reply.assert_not_called()
