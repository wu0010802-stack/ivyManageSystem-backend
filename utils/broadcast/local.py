"""utils/broadcast/local.py — process-local backend（行為等價既有 hub）。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

from utils.broadcast import BroadcastBackend
from utils.ws_hub import BROADCAST_RETRY_DELAY, MAX_BROADCAST_RETRIES

logger = logging.getLogger(__name__)


class LocalBackend(BroadcastBackend):
    def __init__(self, *, payload_max_bytes: int = 8192):
        self._subscribers: dict[str, list[WebSocket]] = defaultdict(list)
        self._payload_max_bytes = payload_max_bytes

    async def publish(self, channel: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str)
        if len(body.encode("utf-8")) > self._payload_max_bytes:
            raise ValueError(
                f"broadcast payload too large for channel={channel}: "
                f"{len(body)} bytes > {self._payload_max_bytes}"
            )
        await self._dispatch_local(channel, body)

    def subscribe(self, channel: str, ws: WebSocket) -> None:
        self._subscribers[channel].append(ws)

    def unsubscribe(self, ws: WebSocket) -> None:
        for lst in self._subscribers.values():
            if ws in lst:
                lst.remove(ws)

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def _dispatch_local(self, channel: str, body: str) -> None:
        targets = list(self._subscribers.get(channel, []))
        dead: list[WebSocket] = []
        for ws in targets:
            sent = False
            for attempt in range(1, MAX_BROADCAST_RETRIES + 1):
                try:
                    await ws.send_text(body)
                    sent = True
                    break
                except Exception as exc:
                    if attempt < MAX_BROADCAST_RETRIES:
                        await asyncio.sleep(BROADCAST_RETRY_DELAY)
                    else:
                        logger.warning(
                            "broadcast send 失敗 channel=%s attempt=%d: %s",
                            channel,
                            attempt,
                            exc,
                        )
            if not sent:
                dead.append(ws)
        for ws in dead:
            self.unsubscribe(ws)
