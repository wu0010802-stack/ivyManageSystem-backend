"""
tests/test_ws_heartbeat.py — WebSocket 心跳與廣播重試測試

測試範圍：
- _run_connection：定期送 ping、逾時主動關閉、disconnect 呼叫 cleanup
- DismissalConnectionManager.broadcast：重試機制、全部失敗後移除僵死連線
"""

import os
import sys
import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import WebSocketDisconnect
from api.dismissal_ws import (
    _run_connection,
    DismissalConnectionManager,
    MAX_BROADCAST_RETRIES,
)


# ---------------------------------------------------------------------------
# 輔助
# ---------------------------------------------------------------------------

def _make_ws(*, recv_side_effect=None, send_raises=None):
    """建立最小化 WS mock。

    recv_side_effect：傳入 async callable，用來控制 receive_text 行為。
    send_raises      ：若指定，send_text 將拋出該例外（AsyncMock side_effect）。
    """
    ws = MagicMock()
    ws.close = AsyncMock()

    if recv_side_effect is None:
        async def _block():
            await asyncio.sleep(1000)   # 預設：永遠不回應
        ws.receive_text = _block
    else:
        ws.receive_text = recv_side_effect

    if send_raises:
        ws.send_text = AsyncMock(side_effect=send_raises)
    else:
        ws.send_text = AsyncMock()

    return ws


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# TestRunConnectionHeartbeat
# ---------------------------------------------------------------------------

class TestRunConnectionHeartbeat:

    def test_sends_ping_periodically(self):
        """每隔 ping_interval 應向 client 送 {"type":"ping"}。"""
        ping_count = 0

        async def _track_send(msg):
            nonlocal ping_count
            if '"type":"ping"' in msg:
                ping_count += 1

        ws = _make_ws()
        ws.send_text = _track_send   # 替換為計數 async func

        async def _run_test():
            task = asyncio.create_task(
                _run_connection(ws, ping_interval=0.01, pong_timeout=100.0)
            )
            await asyncio.sleep(0.06)   # ~6 ping 機會
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        _run(_run_test())
        assert ping_count >= 4, f"預期至少 4 次 ping，實際 {ping_count}"

    def test_cleanup_called_on_normal_disconnect(self):
        """WebSocketDisconnect 正常斷線後 cleanup 應被呼叫一次。"""
        async def _disconnect():
            raise WebSocketDisconnect(code=1000)

        ws = _make_ws(recv_side_effect=_disconnect)
        cleanup = MagicMock()

        _run(_run_connection(ws, cleanup=cleanup, ping_interval=10.0, pong_timeout=10.0))
        cleanup.assert_called_once()

    def test_cleanup_called_on_pong_timeout(self):
        """超過 pong_timeout 秒無任何訊息時 cleanup 應被呼叫，且 ws.close 被執行。"""
        ws = _make_ws()    # receive_text 預設永遠不回應
        cleanup = MagicMock()

        _run(_run_connection(ws, cleanup=cleanup, ping_interval=10.0, pong_timeout=0.03))
        cleanup.assert_called_once()
        ws.close.assert_called_once()

    def test_cleanup_called_when_ping_fails(self):
        """send_text 失敗（ping 送不出去）時 cleanup 應被呼叫。"""
        ws = _make_ws(send_raises=OSError("broken pipe"))
        cleanup = MagicMock()

        async def _run_test():
            task = asyncio.create_task(
                _run_connection(ws, cleanup=cleanup, ping_interval=0.01, pong_timeout=100.0)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        _run(_run_test())
        cleanup.assert_called_once()

    def test_no_error_when_cleanup_is_none(self):
        """cleanup=None 時不應拋出例外。"""
        async def _disconnect():
            raise WebSocketDisconnect(code=1000)

        ws = _make_ws(recv_side_effect=_disconnect)
        # 不應拋出 TypeError / AttributeError
        _run(_run_connection(ws, cleanup=None, ping_interval=10.0, pong_timeout=10.0))

    def test_ping_not_sent_to_already_closed_connection(self):
        """斷線後 ping_task 取消，send_text 呼叫次數不會無限增長。"""
        async def _disconnect():
            raise WebSocketDisconnect(code=1000)

        ws = _make_ws(recv_side_effect=_disconnect)
        _run(_run_connection(ws, ping_interval=0.001, pong_timeout=10.0))
        # 斷線後 ping_task 應被 cancel，send_text 呼叫應極少（≤1）
        assert ws.send_text.call_count <= 1


# ---------------------------------------------------------------------------
# TestBroadcastRetry
# ---------------------------------------------------------------------------

class TestBroadcastRetry:

    def _build_manager_with_admin(self, ws) -> DismissalConnectionManager:
        mgr = DismissalConnectionManager()
        mgr._admin_conns.append(ws)
        return mgr

    def test_success_on_first_attempt_no_retry(self):
        """第一次就成功時不應重試。"""
        ws = AsyncMock()
        mgr = self._build_manager_with_admin(ws)
        _run(mgr.broadcast(1, {"type": "test"}))
        assert ws.send_text.call_count == 1

    def test_retries_on_transient_failure(self):
        """第一次失敗、第二次成功時應只呼叫 send_text 兩次且連線不被移除。"""
        ws = AsyncMock()
        ws.send_text.side_effect = [OSError("transient"), None]   # 第一次失敗，第二次成功
        mgr = self._build_manager_with_admin(ws)

        _run(mgr.broadcast(1, {"type": "test"}))

        assert ws.send_text.call_count == 2
        assert ws in mgr._admin_conns, "一次重試後成功，不應被移除"

    def test_marks_dead_after_all_retries_exhausted(self):
        """全部重試都失敗時連線應被移除。"""
        ws = AsyncMock()
        ws.send_text.side_effect = OSError("dead connection")
        mgr = self._build_manager_with_admin(ws)

        _run(mgr.broadcast(1, {"type": "test"}))

        assert ws.send_text.call_count == MAX_BROADCAST_RETRIES
        assert ws not in mgr._admin_conns, "全部失敗後應從 admin_conns 移除"

    def test_successful_connections_unaffected_by_dead_one(self):
        """同時廣播時，成功的連線不應受僵死連線影響。"""
        dead_ws = AsyncMock()
        dead_ws.send_text.side_effect = OSError("dead")
        live_ws = AsyncMock()

        mgr = DismissalConnectionManager()
        mgr._admin_conns.extend([dead_ws, live_ws])

        _run(mgr.broadcast(1, {"type": "test"}))

        assert dead_ws not in mgr._admin_conns
        assert live_ws in mgr._admin_conns
        live_ws.send_text.assert_called_once()

    def test_broadcast_to_teacher_and_admin(self):
        """廣播應同時送給指定班級的老師連線與全部管理連線。"""
        teacher_ws = AsyncMock()
        admin_ws = AsyncMock()

        mgr = DismissalConnectionManager()
        mgr._teacher_conns[42].append(teacher_ws)
        mgr._admin_conns.append(admin_ws)

        _run(mgr.broadcast(42, {"type": "dismissal_call_created"}))

        teacher_ws.send_text.assert_called_once()
        admin_ws.send_text.assert_called_once()

    def test_broadcast_other_classroom_teacher_not_notified(self):
        """不在指定班級的老師連線不應收到廣播。"""
        teacher_ws = AsyncMock()
        mgr = DismissalConnectionManager()
        mgr._teacher_conns[99].append(teacher_ws)   # 班級 99，非廣播目標

        _run(mgr.broadcast(42, {"type": "test"}))   # 廣播給班級 42

        teacher_ws.send_text.assert_not_called()
