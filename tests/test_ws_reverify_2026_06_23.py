"""P2-8 回歸（2026-06-23 全系統資安掃描）：WS 連線建立後須週期性重驗 token。

run_ws_connection handshake 後只有 ping/recv 兩個 loop，不再驗證 token →
登出（token_version bump）/ 停用（is_active=False）/ 改密 / jti blocklist 後，
既有 WS 連線仍持續串流即時 PII，唯一關閉路徑是 graceful shutdown。

修法：run_ws_connection 加可選 verify 回調 + verify_interval，週期性重驗，
失敗即 ws.close（把暴露窗口從「無限期」降到 ≤ verify_interval）。

純 asyncio 單元測試（MockWS，無 DB / 無真 WS）。
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.ws_hub import WS_CLOSE_INVALID_TOKEN, run_ws_connection


class _MockWS:
    def __init__(self):
        self.closed = False
        self.close_code = None
        self.sent: list[str] = []

    async def send_text(self, msg: str):
        self.sent.append(msg)

    async def receive_text(self) -> str:
        await asyncio.sleep(3600)  # 測試期內不完成（不讓 recv_loop 先結束）
        return ""

    async def close(self, code=None, reason=None):
        self.closed = True
        self.close_code = code


def test_reverify_failure_closes_connection():
    """verify 回調回 False（token 已撤銷）→ 連線被主動關閉。"""

    async def _run():
        ws = _MockWS()
        await run_ws_connection(
            ws,
            verify=lambda: False,
            verify_interval=0.01,
            ping_interval=3600,
            pong_timeout=3600,
        )
        return ws

    ws = asyncio.run(_run())
    assert ws.closed, "重驗失敗應主動關閉 WS 連線"
    assert ws.close_code == WS_CLOSE_INVALID_TOKEN


def test_reverify_success_then_revoked_closes():
    """先重驗通過數次，之後撤銷 → 仍會在下一次重驗關閉。"""
    results = [True, True, False]

    def verify():
        return results.pop(0) if results else False

    async def _run():
        ws = _MockWS()
        await run_ws_connection(
            ws,
            verify=verify,
            verify_interval=0.01,
            ping_interval=3600,
            pong_timeout=3600,
        )
        return ws

    ws = asyncio.run(_run())
    assert ws.closed
    assert not results, "verify 應被持續呼叫直到撤銷"


def test_no_verify_backward_compat():
    """不傳 verify → 行為不變（無重驗 loop；recv timeout 仍正常結束）。"""

    async def _run():
        ws = _MockWS()
        await run_ws_connection(ws, ping_interval=3600, pong_timeout=0.02)
        return ws

    ws = asyncio.run(_run())
    assert ws.closed, "recv timeout 應正常關閉（與重驗無關）"
