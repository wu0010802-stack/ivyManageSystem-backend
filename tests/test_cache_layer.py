"""tests/test_cache_layer.py — MemoryCache + get_cache singleton 測試。"""

import time

import pytest

from utils.cache_layer import (
    Cache,
    MemoryCache,
    get_cache,
    reset_cache_for_testing,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_cache_for_testing()
    yield
    reset_cache_for_testing()


class TestMemoryCacheBasic:
    def test_get_returns_none_when_missing(self):
        cache = MemoryCache()
        assert cache.get("ns1", "k1") is None

    def test_set_then_get_round_trips(self):
        cache = MemoryCache()
        cache.set("ns1", "k1", {"x": 1}, ttl=60)
        assert cache.get("ns1", "k1") == {"x": 1}

    def test_set_preserves_object_identity_inprocess(self):
        """in-process driver 直接擺 obj，回的就是同一個 ref（與 Redis driver 不同）"""
        cache = MemoryCache()
        obj = [1, 2, 3]
        cache.set("ns1", "k1", obj, ttl=60)
        assert cache.get("ns1", "k1") is obj

    def test_delete_removes_key(self):
        cache = MemoryCache()
        cache.set("ns1", "k1", "v1", ttl=60)
        cache.delete("ns1", "k1")
        assert cache.get("ns1", "k1") is None

    def test_delete_missing_key_is_noop(self):
        cache = MemoryCache()
        # 不應拋例外
        cache.delete("ns1", "nope")


class TestNamespaceIsolation:
    def test_same_key_different_namespaces_dont_collide(self):
        cache = MemoryCache()
        cache.set("ns1", "k", "v1", ttl=60)
        cache.set("ns2", "k", "v2", ttl=60)
        assert cache.get("ns1", "k") == "v1"
        assert cache.get("ns2", "k") == "v2"

    def test_clear_namespace_only_clears_target(self):
        cache = MemoryCache()
        cache.set("ns1", "a", 1, ttl=60)
        cache.set("ns1", "b", 2, ttl=60)
        cache.set("ns2", "c", 3, ttl=60)

        cleared = cache.clear_namespace("ns1")
        assert cleared == 2
        assert cache.get("ns1", "a") is None
        assert cache.get("ns1", "b") is None
        assert cache.get("ns2", "c") == 3

    def test_clear_unknown_namespace_returns_zero(self):
        cache = MemoryCache()
        assert cache.clear_namespace("nope") == 0


class TestTTLExpiry:
    def test_get_returns_none_after_ttl(self):
        cache = MemoryCache()
        cache.set("ns1", "k1", "v1", ttl=1)
        assert cache.get("ns1", "k1") == "v1"
        time.sleep(1.1)
        assert cache.get("ns1", "k1") is None

    def test_per_namespace_ttl_independent(self):
        cache = MemoryCache()
        cache.set("short", "k", "v", ttl=1)
        cache.set("long", "k", "v", ttl=10)
        time.sleep(1.1)
        assert cache.get("short", "k") is None
        assert cache.get("long", "k") == "v"


class TestSingleton:
    def test_get_cache_returns_same_instance(self):
        c1 = get_cache()
        c2 = get_cache()
        assert c1 is c2

    def test_reset_for_testing_creates_new_instance(self):
        c1 = get_cache()
        reset_cache_for_testing()
        c2 = get_cache()
        assert c1 is not c2

    def test_get_cache_default_backend_is_memory(self):
        cache = get_cache()
        assert isinstance(cache, MemoryCache)


class TestProtocol:
    def test_memory_cache_satisfies_protocol(self):
        cache: Cache = MemoryCache()  # 靜態型別檢查
        cache.set("ns", "k", "v", ttl=60)
        assert cache.get("ns", "k") == "v"
