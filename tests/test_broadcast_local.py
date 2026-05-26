"""LocalBackend unit tests — subscribe/publish/unsubscribe + dead WS sweep."""

import asyncio

import pytest

from utils.broadcast.local import LocalBackend


class FakeWS:
    """Mock WebSocket — 紀錄收到的 text；可設成 raise 模擬死亡連線。"""

    def __init__(self, *, fail: bool = False):
        self.received: list[str] = []
        self.fail = fail
        self.send_count = 0

    async def send_text(self, body: str) -> None:
        self.send_count += 1
        if self.fail:
            raise RuntimeError("simulated dead ws")
        self.received.append(body)


def test_publish_routes_to_channel_subscribers():
    async def _go():
        b = LocalBackend(payload_max_bytes=8192)
        ws_a = FakeWS()
        ws_b = FakeWS()
        b.subscribe("ch1", ws_a)
        b.subscribe("ch2", ws_b)

        await b.publish("ch1", {"type": "hello", "data": 1})

        assert len(ws_a.received) == 1
        assert '"hello"' in ws_a.received[0]
        assert len(ws_b.received) == 0

    asyncio.run(_go())


def test_publish_to_empty_channel_no_error():
    async def _go():
        b = LocalBackend(payload_max_bytes=8192)
        await b.publish("nobody-listens", {"x": 1})  # 不該 raise

    asyncio.run(_go())


def test_unsubscribe_removes_from_all_channels():
    async def _go():
        b = LocalBackend(payload_max_bytes=8192)
        ws = FakeWS()
        b.subscribe("ch1", ws)
        b.subscribe("ch2", ws)
        b.unsubscribe(ws)

        await b.publish("ch1", {"x": 1})
        await b.publish("ch2", {"x": 2})
        assert ws.received == []

    asyncio.run(_go())


def test_dead_ws_swept_after_retry():
    async def _go():
        b = LocalBackend(payload_max_bytes=8192)
        ws_dead = FakeWS(fail=True)
        ws_alive = FakeWS()
        b.subscribe("ch", ws_dead)
        b.subscribe("ch", ws_alive)

        await b.publish("ch", {"x": 1})

        # ws_dead 嘗試 MAX_BROADCAST_RETRIES 次後被掃掉
        from utils.ws_hub import MAX_BROADCAST_RETRIES

        assert ws_dead.send_count == MAX_BROADCAST_RETRIES
        assert len(ws_alive.received) == 1

        # 第二次 publish — dead 應已被 unsubscribe，不再嘗試
        await b.publish("ch", {"x": 2})
        assert ws_dead.send_count == MAX_BROADCAST_RETRIES  # 不變
        assert len(ws_alive.received) == 2

    asyncio.run(_go())


def test_publish_payload_size_guard():
    async def _go():
        b = LocalBackend(payload_max_bytes=100)
        ws = FakeWS()
        b.subscribe("ch", ws)
        big = {"x": "a" * 200}
        with pytest.raises(ValueError, match="too large"):
            await b.publish("ch", big)

    asyncio.run(_go())


def test_start_stop_noop():
    async def _go():
        b = LocalBackend()
        await b.start()
        await b.stop()  # 不該 raise

    asyncio.run(_go())


def test_publish_many_uses_publish():
    """publish_many 走 publish 路徑（驗證 base class default impl 整合 OK）。"""

    async def _go():
        b = LocalBackend()
        ws = FakeWS()
        b.subscribe("a", ws)
        b.subscribe("b", ws)
        await b.publish_many(["a", "b"], {"x": 1})
        assert len(ws.received) == 2

    asyncio.run(_go())
