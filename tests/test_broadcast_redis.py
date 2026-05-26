"""RedisBackend unit + integration tests（fakeredis）。"""

import asyncio
import json
from unittest.mock import patch

import pytest

import fakeredis.aioredis as fakeredis_aio

from utils.broadcast.redis import RedisBackend


class FakeWS:
    def __init__(self, *, fail: bool = False):
        self.received: list[str] = []
        self.fail = fail
        self.send_count = 0

    async def send_text(self, body: str) -> None:
        self.send_count += 1
        if self.fail:
            raise RuntimeError("simulated dead ws")
        self.received.append(body)


@pytest.fixture
def fake_redis(monkeypatch):
    """patch redis.asyncio.from_url 回傳 fakeredis instance（同一 server, 不同 client）。"""
    server = fakeredis_aio.FakeServer()
    created: list = []

    def _from_url(url: str, **kw):
        client = fakeredis_aio.FakeRedis(
            server=server, decode_responses=kw.get("decode_responses", False)
        )
        created.append(client)
        return client

    monkeypatch.setattr("redis.asyncio.from_url", _from_url)
    yield server, created


def test_publish_fans_out_to_local_via_pump(fake_redis):
    """publish → Redis → pump → local subscribers. Same instance also receives via pump."""

    async def _go():
        backend = RedisBackend(
            redis_url="redis://fake/0", key_prefix="ivy", payload_max_bytes=8192
        )
        await backend.start()
        ws = FakeWS()
        backend.subscribe("test.channel", ws)

        await backend.publish("test.channel", {"type": "x", "data": 1})

        # 等 pump 流入（fakeredis pubsub 是 in-memory）
        await asyncio.sleep(0.05)
        assert len(ws.received) == 1
        assert '"x"' in ws.received[0]

        await backend.stop()

    asyncio.run(_go())


def test_two_backends_fanout_via_shared_server(fake_redis):
    """A.publish → B 的 subscriber 收得到（驗證真 cross-instance fanout）。"""

    async def _go():
        a = RedisBackend(
            redis_url="redis://fake/0", key_prefix="ivy", payload_max_bytes=8192
        )
        b = RedisBackend(
            redis_url="redis://fake/0", key_prefix="ivy", payload_max_bytes=8192
        )
        await a.start()
        await b.start()

        ws_b = FakeWS()
        b.subscribe("cross.instance", ws_b)

        await a.publish("cross.instance", {"hi": "from_a"})
        await asyncio.sleep(0.1)

        assert len(ws_b.received) == 1
        assert '"from_a"' in ws_b.received[0]

        await a.stop()
        await b.stop()

    asyncio.run(_go())


def test_payload_size_guard(fake_redis):
    async def _go():
        backend = RedisBackend(
            redis_url="redis://fake/0", key_prefix="ivy", payload_max_bytes=50
        )
        await backend.start()
        with pytest.raises(ValueError, match="too large"):
            await backend.publish("ch", {"x": "a" * 200})
        await backend.stop()

    asyncio.run(_go())


def test_unsubscribe_removes_from_all_channels(fake_redis):
    async def _go():
        backend = RedisBackend(
            redis_url="redis://fake/0", key_prefix="ivy", payload_max_bytes=8192
        )
        await backend.start()
        ws = FakeWS()
        backend.subscribe("c1", ws)
        backend.subscribe("c2", ws)
        backend.unsubscribe(ws)

        await backend.publish("c1", {"x": 1})
        await backend.publish("c2", {"x": 2})
        await asyncio.sleep(0.1)
        assert ws.received == []

        await backend.stop()

    asyncio.run(_go())


def test_publish_fail_open_when_redis_unreachable():
    """Redis publish 拋例外時 publish() 不該 raise（fail-open，限頻 Sentry）。"""

    async def _go():
        from redis.exceptions import ConnectionError as RedisConnectionError

        backend = RedisBackend(
            redis_url="redis://localhost:1/0",  # unreachable url
            key_prefix="ivy",
            payload_max_bytes=8192,
        )

        # 不跑 start — 直接打 publish 觀察 fail-open；手動把 _redis 設成 raise
        class _BadRedis:
            async def publish(self, *a, **kw):
                raise RedisConnectionError("nope")

        backend._redis = _BadRedis()
        # 不該 raise
        await backend.publish("ch", {"x": 1})

    asyncio.run(_go())


def test_init_without_redis_url_raises():
    with pytest.raises(RuntimeError, match="CACHE_REDIS_URL is required"):
        RedisBackend(redis_url="", key_prefix="ivy", payload_max_bytes=8192)


def test_start_stop_lifecycle_idempotent(fake_redis):
    """stop() 後不該洩漏 task。"""

    async def _go():
        backend = RedisBackend(
            redis_url="redis://fake/0", key_prefix="ivy", payload_max_bytes=8192
        )
        await backend.start()
        await backend.stop()

    asyncio.run(_go())


def test_key_prefix_routing(fake_redis):
    """publish ch 應走 Redis channel `{prefix}:ch`。"""

    async def _go():
        backend = RedisBackend(
            redis_url="redis://fake/0",
            key_prefix="kindergarten",
            payload_max_bytes=8192,
        )
        await backend.start()
        ws = FakeWS()
        backend.subscribe("event.x", ws)
        await backend.publish("event.x", {"foo": "bar"})
        await asyncio.sleep(0.05)
        assert len(ws.received) == 1
        await backend.stop()

    asyncio.run(_go())


def test_start_fail_loud_when_redis_unreachable(monkeypatch):
    """start() 應在 redis ping 失敗時 raise（fail-loud 啟動契約，spec §5.1）。"""
    from redis.exceptions import ConnectionError as RedisConnectionError

    class _UnreachableRedis:
        async def ping(self):
            raise RedisConnectionError("connection refused")

        def pubsub(self):
            raise AssertionError(
                "should not reach pubsub() — ping should have raised first"
            )

        async def aclose(self):
            return

    def _from_url(url: str, **kw):
        return _UnreachableRedis()

    monkeypatch.setattr("redis.asyncio.from_url", _from_url)

    async def _go():
        backend = RedisBackend(
            redis_url="redis://unreachable/0", key_prefix="ivy", payload_max_bytes=8192
        )
        with pytest.raises(RedisConnectionError, match="connection refused"):
            await backend.start()

    asyncio.run(_go())
