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


UNREGISTERED_EVENT = "test.unknown_unregistered_event"
# Phase 4 Section 2 後 LINE_HANDLERS 覆蓋全部 23 個 event。用一個故意不存在的
# event_type 驗 LineAdapter fallback path（safety net for 未來新 event）。


def test_line_adapter_fallback_push_text_for_unmapped_event_type():
    """LINE_HANDLERS 沒對映 event_type 時走 fallback push_text_to_user（safety net）。"""
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
    """LineAdapter fallback path 收到非 str recipient_user_id 且非 group_mode
    應 raise ValueError。"""
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


def test_line_handlers_covers_all_23_events():
    """Phase 4 Section 2 後 LINE_HANDLERS 覆蓋全部 23 個 event_type（含 dismissal.created
    via group_id mode）。fallback push_text path 為 safety net 給未來新 event。"""
    assert UNREGISTERED_EVENT not in LINE_HANDLERS
    # 員工 11 + 家長 7 + 才藝家長 3 + Growth Report 1 + dismissal 1 = 23
    assert len(LINE_HANDLERS) == 23


def test_dismissal_created_handler_uses_group_push():
    """dismissal.created handler 走 push_text_to_group(line_service._target_id)
    LINE 群組推送，不走個人 push_to_user。"""
    fake_ls = MagicMock()
    fake_ls._target_id = "C_dismissal_group"
    adapter = LineAdapter(fake_ls)
    # Section 2 caller (dismissal_calls.py) 傳 line_group_id="" sentinel
    evt = PendingEvent(
        event_type="dismissal.created",
        recipient_user_id=None,
        context={
            "student_name": "小明",
            "classroom_name": "向日葵班",
            "note": "今日提早接送",
        },
        sender_id=None,
        source_entity_type="dismissal_call",
        source_entity_id=99,
        channels=("line", "ws"),
        line_group_id="",
    )
    adapter.send(evt, Rendered(title="t", body="b", deep_link=None), log_id=1)
    fake_ls.push_text_to_group.assert_called_once()
    args = fake_ls.push_text_to_group.call_args[0]
    assert args[0] == "C_dismissal_group"  # fallback 用 ls._target_id
    # Spec E (P0 #6) build_dismissal_message 去識別化：
    # 不再含 student_name / classroom_name；改用「您的孩子」+ note 仍保留
    assert "小明" not in args[1]
    assert "向日葵班" not in args[1]
    assert "您的孩子" in args[1]
    assert "今日提早接送" in args[1]  # note 仍保留
    fake_ls._push_to_user.assert_not_called()


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
