"""Test get_broadcast() factory + lru_cache + settings 切換。"""

import asyncio
import os

import pytest

from utils.broadcast import get_broadcast, reset_for_tests


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()


def test_factory_default_returns_local(monkeypatch):
    monkeypatch.setenv("CACHE_BACKEND", "memory")
    from config import reset_for_tests as cfg_reset

    cfg_reset()
    backend = get_broadcast()
    assert backend.__class__.__name__ == "LocalBackend"


def test_factory_returns_singleton(monkeypatch):
    monkeypatch.setenv("CACHE_BACKEND", "memory")
    from config import reset_for_tests as cfg_reset

    cfg_reset()
    b1 = get_broadcast()
    b2 = get_broadcast()
    assert b1 is b2


def test_factory_redis_mode(monkeypatch):
    monkeypatch.setenv("CACHE_BACKEND", "redis")
    monkeypatch.setenv("CACHE_REDIS_URL", "redis://localhost:6379/0")
    from config import reset_for_tests as cfg_reset

    cfg_reset()
    backend = get_broadcast()
    assert backend.__class__.__name__ == "RedisBackend"


def test_factory_broadcast_backend_overrides_cache_backend(monkeypatch):
    monkeypatch.setenv("CACHE_BACKEND", "memory")
    monkeypatch.setenv("BROADCAST_BACKEND", "redis")
    monkeypatch.setenv("CACHE_REDIS_URL", "redis://localhost:6379/0")
    from config import reset_for_tests as cfg_reset

    cfg_reset()
    backend = get_broadcast()
    assert backend.__class__.__name__ == "RedisBackend"


def test_publish_many_dedupes_channel_keys(monkeypatch):
    """publish_many should dedupe channel keys (no double-push to same ch)."""
    monkeypatch.setenv("CACHE_BACKEND", "memory")
    from config import reset_for_tests as cfg_reset

    cfg_reset()
    backend = get_broadcast()
    # 用 in-memory recorder 監聽 publish 呼叫次數
    calls: list[str] = []

    async def fake_publish(channel, payload):
        calls.append(channel)

    backend.publish = fake_publish  # type: ignore[method-assign]

    asyncio.run(backend.publish_many(["a", "b", "a", "c", "b"], {"x": 1}))
    assert calls == ["a", "b", "c"]
