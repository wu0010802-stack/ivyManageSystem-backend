"""utils/broadcast — 跨 instance WebSocket 廣播 backend。

LocalBackend：process-local，行為等價既有 hub（dev / memory mode prod）。
RedisBackend：Redis Pub/Sub fanout，subscribe 端各 instance 啟一個 pump task。

caller 統一透過 get_broadcast() 取得 singleton（lru_cache）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache

from fastapi import WebSocket


class BroadcastBackend(ABC):
    @abstractmethod
    async def publish(self, channel: str, payload: dict) -> None:
        """跨 instance 廣播至此 channel 的所有訂閱者。

        Fail-open：Redis 失敗時仍會 push 給同 instance subscribers。
        Payload > publish_payload_max_bytes 直接 raise ValueError。
        """

    async def publish_many(self, channels: list[str], payload: dict) -> None:
        """syntactic sugar — channel-key 去重後逐一 publish。

        WS-level 去重（同 WS 訂多 channel 只收一次）未實作（YAGNI）。
        實務上 caller 不會把同 WS 訂跨類型 channel。
        """
        seen: dict[str, None] = {}
        for ch in channels:
            if ch in seen:
                continue
            seen[ch] = None
            await self.publish(ch, payload)

    @abstractmethod
    def subscribe(self, channel: str, ws: WebSocket) -> None:
        """把 ws 加入 channel 訂閱清單（同步，無 I/O）。"""

    @abstractmethod
    def unsubscribe(self, ws: WebSocket) -> None:
        """從所有 channel 移除 ws。"""

    @abstractmethod
    async def start(self) -> None:
        """lifespan startup hook — 建 connection pool、啟 pump task。"""

    @abstractmethod
    async def stop(self) -> None:
        """lifespan shutdown hook — graceful drain pump、close connections。"""


@lru_cache(maxsize=1)
def get_broadcast() -> BroadcastBackend:
    from config import settings

    cache = settings.cache
    if cache.backend == "redis":
        from utils.broadcast.redis import RedisBackend

        return RedisBackend(
            redis_url=cache.redis_url,
            key_prefix=cache.key_prefix,
            payload_max_bytes=cache.publish_payload_max_bytes,
        )
    from utils.broadcast.local import LocalBackend

    return LocalBackend(payload_max_bytes=cache.publish_payload_max_bytes)


def reset_for_tests() -> None:
    """test fixture 用 — 清掉 lru_cache 讓下次 get_broadcast() 重建。

    呼叫前若 backend 已 start() 過，caller 須自行 await stop()。
    """
    get_broadcast.cache_clear()
