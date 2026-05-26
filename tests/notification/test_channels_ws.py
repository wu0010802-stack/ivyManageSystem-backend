"""WS adapter + inbox WS push 同步 wrapper 測試。"""

import asyncio
import threading
import time
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from services.notification.dispatch import PendingEvent
from services.notification.renderers import Rendered
from services.notification._channels import ws as ws_adapter_mod


@pytest.fixture
def fake_loop():
    """提供一個跑著的 loop 供 run_coroutine_threadsafe，並 patch get_main_loop。"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    with patch("services.notification._channels.ws.get_main_loop", return_value=loop):
        yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)


def _evt(event_type, **kwargs):
    return PendingEvent(
        event_type=event_type,
        recipient_user_id=kwargs.get("recipient_user_id", 1),
        context=kwargs.get("context", {}),
        sender_id=None,
        source_entity_type=None,
        source_entity_id=None,
        channels=kwargs.get("channels", ("line", "ws")),
    )


def test_ws_adapter_raises_when_loop_unregistered():
    with patch("services.notification._channels.ws.get_main_loop", return_value=None):
        adapter = ws_adapter_mod.WsAdapter()
        with pytest.raises(RuntimeError, match="loop"):
            adapter.send(
                _evt("parent.announcement"),
                Rendered(title="t", body="b", deep_link=None),
                log_id=1,
            )


def test_ws_adapter_parent_event_calls_broadcast_parent(fake_loop):
    with patch(
        "services.notification._channels.ws.broadcast_parent", new=AsyncMock()
    ) as mock_bp:
        adapter = ws_adapter_mod.WsAdapter()
        adapter.send(
            _evt("parent.message_received", recipient_user_id=42),
            Rendered(title="t", body="b", deep_link="/x"),
            log_id=99,
        )
        time.sleep(0.1)  # 給 async mock 時間在 fake_loop thread 被 awaited
        mock_bp.assert_awaited_once()
        args = mock_bp.call_args[0]
        assert args[0] == 42  # parent user_id
        payload = args[1]
        assert payload["log_id"] == 99
        assert payload["event_type"] == "parent.message_received"


def test_ws_adapter_dismissal_calls_classroom_broadcast(fake_loop):
    with patch.object(ws_adapter_mod, "dismissal_manager") as mock_mgr:
        mock_mgr.broadcast = AsyncMock()
        adapter = ws_adapter_mod.WsAdapter()
        adapter.send(
            _evt(
                "dismissal.created", recipient_user_id=None, context={"classroom_id": 7}
            ),
            Rendered(title="t", body="b", deep_link=None),
            log_id=1,
        )
        time.sleep(0.1)
        mock_mgr.broadcast.assert_awaited_once()
        args = mock_mgr.broadcast.call_args[0]
        assert args[0] == 7  # classroom_id


def test_ws_adapter_unsupported_event_type_raises(fake_loop):
    """員工 inbox event 不應走 ws adapter（應走 _inbox_ws_push）。"""
    adapter = ws_adapter_mod.WsAdapter()
    with pytest.raises(RuntimeError, match="不支援"):
        adapter.send(
            _evt("leave.approved", channels=("in_app", "line")),
            Rendered(title="t", body="b", deep_link=None),
            log_id=1,
        )


def test_inbox_ws_push_calls_inbox_broadcast_user(fake_loop):
    with patch("api.inbox_ws.inbox_broadcast_user", new=AsyncMock()) as mock_bcast:
        ws_adapter_mod._inbox_ws_push(
            _evt("leave.approved", recipient_user_id=42),
            Rendered(title="t", body="b", deep_link="/x"),
            log_id=99,
        )
        time.sleep(0.1)
        mock_bcast.assert_awaited_once()
        args = mock_bcast.call_args[0]
        assert args[0] == 42
        assert args[1]["log_id"] == 99


def test_inbox_ws_push_loop_unregistered_raises():
    with patch("services.notification._channels.ws.get_main_loop", return_value=None):
        with pytest.raises(RuntimeError, match="loop"):
            ws_adapter_mod._inbox_ws_push(
                _evt("leave.approved", recipient_user_id=42),
                Rendered(title="t", body="b", deep_link=None),
                log_id=1,
            )
