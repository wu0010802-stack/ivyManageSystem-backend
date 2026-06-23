"""
utils/ws_hub.py — 泛用 WebSocket channel hub（給 dismissal、聯絡簿等 WS 端點重用）

核心抽象：
- ChannelHub：以任意 hashable key 分組訂閱者；廣播帶重試 + 僵死偵測
- _run_ws_connection：通用心跳與超時主循環

dismissal_ws / contact_book_ws / 其他 WS 端點都應透過 ChannelHub 持有訂閱狀態，
避免每個端點各自 copy ConnectionManager 邏輯。
"""

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from typing import Any, Iterable

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

# WS 自訂關閉碼（4000–4999 為應用程式保留範圍）
WS_CLOSE_MISSING_TOKEN = 4001
WS_CLOSE_INVALID_TOKEN = 4003
WS_CLOSE_FORBIDDEN = 4007

# 心跳與廣播參數
PING_INTERVAL = 30
PONG_TIMEOUT = 90
# P2-8：handshake 後週期性重驗 token 的間隔（秒）。撤銷暴露窗口 ≤ 此值。
WS_REVERIFY_INTERVAL = 60
MAX_BROADCAST_RETRIES = 2
BROADCAST_RETRY_DELAY = 0.05


class ChannelHub:
    """以任意 hashable key 分組的 WebSocket 訂閱中樞。

    使用方式：
        hub = ChannelHub()
        hub.subscribe(("classroom", 12), ws)
        await hub.broadcast([("classroom", 12), "admin"], {"type": "x"})
        hub.unsubscribe(ws)
    """

    def __init__(self) -> None:
        import warnings

        warnings.warn(
            "ChannelHub is deprecated; use utils.broadcast.get_broadcast() "
            "(BroadcastBackend) which supports cross-instance fanout via Redis. "
            "Removal target: PR after notification-dispatch-phase-2a merges.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._subs: dict[Any, list[WebSocket]] = defaultdict(list)

    def subscribe(self, channel_key: Any, ws: WebSocket) -> None:
        self._subs[channel_key].append(ws)

    def unsubscribe(self, ws: WebSocket) -> None:
        for lst in list(self._subs.values()):
            if ws in lst:
                lst.remove(ws)

    def channel_size(self, channel_key: Any) -> int:
        return len(self._subs.get(channel_key, []))

    async def broadcast(self, channel_keys: Iterable[Any], event: dict) -> None:
        """同 event 推送至多個 channel；同一條 ws 只會收到一次（去重）。

        每條連線最多重試 MAX_BROADCAST_RETRIES 次，全失敗即移除。
        """
        msg = json.dumps(event, ensure_ascii=False, default=str)
        seen: set[int] = set()
        targets: list[WebSocket] = []
        for k in channel_keys:
            for ws in self._subs.get(k, []):
                if id(ws) not in seen:
                    seen.add(id(ws))
                    targets.append(ws)

        dead: list[WebSocket] = []
        for ws in targets:
            sent = False
            for attempt in range(1, MAX_BROADCAST_RETRIES + 1):
                try:
                    await ws.send_text(msg)
                    sent = True
                    break
                except Exception as exc:
                    if attempt < MAX_BROADCAST_RETRIES:
                        await asyncio.sleep(BROADCAST_RETRY_DELAY)
                    else:
                        logger.warning(
                            "WS 廣播失敗，標記僵死（event=%s, 嘗試=%d）：%s",
                            event.get("type", "unknown"),
                            attempt,
                            exc,
                        )
            if not sent:
                dead.append(ws)
        for ws in dead:
            self.unsubscribe(ws)


async def run_ws_connection(
    ws: WebSocket,
    cleanup=None,
    *,
    ping_interval: float = PING_INTERVAL,
    pong_timeout: float = PONG_TIMEOUT,
    verify=None,
    verify_interval: float = WS_REVERIFY_INTERVAL,
) -> None:
    """通用 WS 主循環：心跳 + 接收 + 超時偵測 +（可選）週期性 token 重驗。

    - ping_task：每 ping_interval 秒送 {"type":"ping"}
    - recv_task：超過 pong_timeout 秒無任何 client 訊息即關閉
    - verify_task：（P2-8，2026-06-23 資安掃描）若提供 verify 回調，每 verify_interval
      秒重驗一次；回 False 或 raise 即主動 ws.close（WS_CLOSE_INVALID_TOKEN）。
      handshake 後 token 撤銷（登出/停用/改密/jti blocklist）的暴露窗口從「無限期」
      降到 ≤ verify_interval，對齊 REST 端立即失效。
    - cleanup：連線結束（任何原因）皆呼叫
    """

    async def _ping_loop():
        while True:
            await asyncio.sleep(ping_interval)
            try:
                await ws.send_text('{"type":"ping"}')
            except Exception:
                logger.debug("WS ping 失敗，連線可能已斷")
                return

    async def _recv_loop():
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=pong_timeout)
            except asyncio.TimeoutError:
                logger.warning("WS 連線 %ds 無回應，主動關閉", int(pong_timeout))
                with contextlib.suppress(Exception):
                    await ws.close()
                return
            except WebSocketDisconnect:
                return

    async def _verify_loop():
        # P2-8：週期性重驗 token（verify 內部含 exp / jti blocklist / token_version /
        # is_active 檢查）；失敗即關閉連線，落實登出/停用立即生效於 WS 通道。
        while True:
            await asyncio.sleep(verify_interval)
            try:
                ok = verify()
            except Exception:
                ok = False
            if not ok:
                logger.info("WS token 週期重驗失敗，主動關閉連線")
                with contextlib.suppress(Exception):
                    await ws.close(code=WS_CLOSE_INVALID_TOKEN)
                return

    tasks = [
        asyncio.create_task(_ping_loop()),
        asyncio.create_task(_recv_loop()),
    ]
    if verify is not None:
        tasks.append(asyncio.create_task(_verify_loop()))
    try:
        await asyncio.wait(set(tasks), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if cleanup:
            cleanup()


def get_token_from_ws(ws: WebSocket) -> str | None:
    """從同源 WebSocket 請求的 httpOnly Cookie 讀取 access_token。"""
    return ws.cookies.get("access_token")
