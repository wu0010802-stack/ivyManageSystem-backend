"""utils/broadcast/redis.py — Redis Pub/Sub fanout backend。

設計：
- publish 只送到 Redis；pump task 流入後 dispatch 給本地 subscribers
- Real Redis Pub/Sub 會 echo own publish（fakeredis spike 已驗證），
  所以本 instance 的訂閱者也透過 pump 流入收到事件，不會重複也不會漏
- fail-open：Redis 失敗時不 raise，限頻 capture Sentry
- start() 階段 fail-loud（redis_url 缺 / 連不到 → 啟動失敗）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict

import redis.asyncio as aioredis
from fastapi import WebSocket
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)

from utils.broadcast import BroadcastBackend
from utils.ws_hub import BROADCAST_RETRY_DELAY, MAX_BROADCAST_RETRIES

logger = logging.getLogger(__name__)

_SENTRY_THROTTLE_SECONDS = 60


class RedisBackend(BroadcastBackend):
    def __init__(
        self,
        *,
        redis_url: str | None,
        key_prefix: str,
        payload_max_bytes: int,
    ):
        if not redis_url:
            raise RuntimeError("CACHE_REDIS_URL is required when CACHE_BACKEND=redis")
        self._redis_url = redis_url
        self._prefix = f"{key_prefix}:"
        self._payload_max_bytes = payload_max_bytes
        self._subscribers: dict[str, list[WebSocket]] = defaultdict(list)
        self._redis: aioredis.Redis | None = None
        self._pubsub = None
        self._pump_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._last_sentry_ts: float = 0.0

    async def start(self) -> None:
        self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        # 啟動時 ping 一次驗證 Redis 可達（fail-loud）
        await self._redis.ping()
        self._pubsub = self._redis.pubsub()
        await self._pubsub.psubscribe(f"{self._prefix}*")
        self._pump_task = asyncio.create_task(self._pump(), name="broadcast-pump")
        logger.info("RedisBackend started prefix=%s", self._prefix)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._pubsub is not None:
            try:
                await self._pubsub.aclose()
            except Exception as exc:
                logger.warning("pubsub close failed: %s", exc)
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception as exc:
                logger.warning("redis close failed: %s", exc)
        logger.info("RedisBackend stopped")

    async def publish(self, channel: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str)
        if len(body.encode("utf-8")) > self._payload_max_bytes:
            raise ValueError(
                f"broadcast payload too large for channel={channel}: "
                f"{len(body)} bytes > {self._payload_max_bytes}"
            )
        try:
            await self._redis.publish(self._prefix + channel, body)
        except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
            self._note_redis_failure("publish", exc)

    def subscribe(self, channel: str, ws: WebSocket) -> None:
        self._subscribers[channel].append(ws)

    def unsubscribe(self, ws: WebSocket) -> None:
        for lst in self._subscribers.values():
            if ws in lst:
                lst.remove(ws)

    async def _pump(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async for msg in self._pubsub.listen():
                    if msg.get("type") != "pmessage":
                        continue
                    channel = msg["channel"]
                    if isinstance(channel, bytes):
                        channel = channel.decode("utf-8")
                    if channel.startswith(self._prefix):
                        channel = channel[len(self._prefix) :]
                    data = msg["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    await self._dispatch_local(channel, data)
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._note_redis_failure("pump", exc)
                try:
                    await asyncio.sleep(min(backoff, 30))
                except asyncio.CancelledError:
                    break
                backoff *= 2
                try:
                    await self._pubsub.psubscribe(f"{self._prefix}*")
                except Exception:
                    continue

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

    def _note_redis_failure(self, kind: str, exc: BaseException) -> None:
        now = time.monotonic()
        if now - self._last_sentry_ts < _SENTRY_THROTTLE_SECONDS:
            logger.warning("redis %s failed (throttled): %s", kind, exc)
            return
        self._last_sentry_ts = now
        logger.warning("redis %s failed: %s", kind, exc)
        try:
            import sentry_sdk

            sentry_sdk.capture_exception(exc)
        except Exception:
            pass
