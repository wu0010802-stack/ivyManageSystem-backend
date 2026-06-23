"""tests/test_cache_layer.py — MemoryCache + get_cache singleton 測試。"""

import pickle
import time

import fakeredis
import pytest

from utils.cache_layer import (
    Cache,
    MemoryCache,
    RedisCache,
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


class TestRedisCacheBasic:
    @pytest.fixture
    def fake_redis(self, monkeypatch):
        server = fakeredis.FakeServer()
        clients = []

        def _from_url(url: str, *args, **kwargs):
            client = fakeredis.FakeRedis(server=server)
            clients.append(client)
            return client

        monkeypatch.setattr("redis.Redis.from_url", _from_url)
        return server, clients

    def test_set_then_get_round_trips_copy(self, fake_redis):
        cache = RedisCache(redis_url="redis://fake/0", key_prefix="ivy")
        obj = {"x": [1, 2, 3]}
        cache.set("ns1", "k1", obj, ttl=60)

        out = cache.get("ns1", "k1")

        assert out == obj
        assert out is not obj

    def test_namespace_clear_only_clears_target(self, fake_redis):
        cache = RedisCache(redis_url="redis://fake/0", key_prefix="ivy")
        cache.set("ns1", "a", 1, ttl=60)
        cache.set("ns1", "b", 2, ttl=60)
        cache.set("ns2", "a", 3, ttl=60)

        assert cache.clear_namespace("ns1") == 2
        assert cache.get("ns1", "a") is None
        assert cache.get("ns1", "b") is None
        assert cache.get("ns2", "a") == 3

    def test_operations_fail_open(self, monkeypatch):
        cache = RedisCache(redis_url="redis://fake/0", key_prefix="ivy")

        class BadRedis:
            def get(self, *args, **kwargs):
                raise RuntimeError("down")

            def set(self, *args, **kwargs):
                raise RuntimeError("down")

            def delete(self, *args, **kwargs):
                raise RuntimeError("down")

            def scan_iter(self, *args, **kwargs):
                raise RuntimeError("down")

        monkeypatch.setattr(cache, "_client", BadRedis())

        assert cache.get("ns", "k") is None
        cache.set("ns", "k", {"v": 1}, ttl=60)
        cache.delete("ns", "k")
        assert cache.clear_namespace("ns") == 0


class TestRedisCacheSocketTimeout:
    """Redis driver fail-open 只擋例外、擋不住「hang」。建構時必須帶 socket timeout，
    否則 Redis 網路分割／連線 hang 會讓同步 cache 呼叫卡死 request thread，
    threadpool 耗盡——正是 fail-open 想避免的事（Medium，2026-06-23 audit）。"""

    def _capture_from_url(self, monkeypatch):
        captured = {}

        def _from_url(url, *args, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return fakeredis.FakeRedis()

        monkeypatch.setattr("redis.Redis.from_url", _from_url)
        return captured

    def test_from_url_receives_socket_timeouts(self, monkeypatch):
        captured = self._capture_from_url(monkeypatch)

        RedisCache(redis_url="redis://fake/0", key_prefix="ivy")

        kwargs = captured["kwargs"]
        assert kwargs.get("socket_timeout") is not None
        assert kwargs["socket_timeout"] > 0
        assert kwargs.get("socket_connect_timeout") is not None
        assert kwargs["socket_connect_timeout"] > 0
        # timeout 即當 miss，不再 retry 拖更久
        assert kwargs.get("retry_on_timeout") is False

    def test_explicit_socket_timeout_overrides_default(self, monkeypatch):
        captured = self._capture_from_url(monkeypatch)

        RedisCache(redis_url="redis://fake/0", key_prefix="ivy", socket_timeout=2.5)

        assert captured["kwargs"]["socket_timeout"] == 2.5
        assert captured["kwargs"]["socket_connect_timeout"] == 2.5

    def test_build_cache_wires_configured_socket_timeout(self, monkeypatch):
        monkeypatch.setenv("CACHE_BACKEND", "redis")
        monkeypatch.setenv("CACHE_REDIS_URL", "redis://localhost:6379/0")
        captured = self._capture_from_url(monkeypatch)
        from config import reset_for_tests as cfg_reset

        cfg_reset()
        reset_cache_for_testing()
        cache = get_cache()

        assert isinstance(cache, RedisCache)
        from config import settings

        assert (
            captured["kwargs"]["socket_timeout"]
            == settings.cache.pubsub_timeout_seconds
        )


class TestRedisCacheHmac:
    """簽章路徑：prod 帶 hmac_key 時，未通過驗章的 payload 一律不 unpickle。"""

    @pytest.fixture
    def fake_redis(self, monkeypatch):
        server = fakeredis.FakeServer()

        def _from_url(url: str, *args, **kwargs):
            return fakeredis.FakeRedis(server=server)

        monkeypatch.setattr("redis.Redis.from_url", _from_url)
        return server

    def test_signed_value_round_trips(self, fake_redis):
        cache = RedisCache(
            redis_url="redis://fake/0", key_prefix="ivy", hmac_key=b"sekret"
        )
        cache.set("ns", "k", {"a": [1, 2, 3]}, ttl=60)

        assert cache.get("ns", "k") == {"a": [1, 2, 3]}

    def test_tampered_payload_is_treated_as_miss(self, fake_redis):
        cache = RedisCache(
            redis_url="redis://fake/0", key_prefix="ivy", hmac_key=b"sekret"
        )
        cache.set("ns", "k", {"a": 1}, ttl=60)
        # 攻擊者直接寫入無有效 MAC 的惡意 pickle bytes
        forged = pickle.dumps({"evil": True})
        cache._client.set(cache._key("ns", "k"), forged)

        # 驗章失敗 → 當 miss，絕不 unpickle 不可信 bytes（無簽章版會回傳 dict）
        assert cache.get("ns", "k") is None

    def test_value_signed_with_other_key_is_rejected(self, fake_redis):
        attacker = RedisCache(
            redis_url="redis://fake/0", key_prefix="ivy", hmac_key=b"attacker-key"
        )
        attacker.set("ns", "k", {"a": 1}, ttl=60)

        victim = RedisCache(
            redis_url="redis://fake/0", key_prefix="ivy", hmac_key=b"real-key"
        )
        assert victim.get("ns", "k") is None


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

    def test_get_cache_redis_backend(self, monkeypatch):
        monkeypatch.setenv("CACHE_BACKEND", "redis")
        monkeypatch.setenv("CACHE_REDIS_URL", "redis://localhost:6379/0")
        from config import reset_for_tests as cfg_reset

        cfg_reset()
        cache = get_cache()
        assert isinstance(cache, RedisCache)


class TestProtocol:
    def test_memory_cache_satisfies_protocol(self):
        cache: Cache = MemoryCache()  # 靜態型別檢查
        cache.set("ns", "k", "v", ttl=60)
        assert cache.get("ns", "k") == "v"
