"""utils/cache_layer.py — 統一 cache facade。

Why: 此前各 router/service 各自 `from cachetools import TTLCache` 建私有
cache，invalidate API 不一致、未來無法 atomic 切 Redis backend。本檔
提供 driver-agnostic 介面：

- `get_cache()` — singleton；依 `settings.cache.backend` 選擇 driver
- `MemoryCache` — in-process driver，包 cachetools.TTLCache
- `RedisCache` — shared Redis driver for multi-worker deployments
- `reset_cache_for_testing()` — 測試用，清掉 singleton 與所有 namespace

關鍵設計：
- 同步 API（非 async），避免 11 個 sync callsite 傳染成 async
- Namespace 為一級概念：`clear_namespace(ns)` 一行清乾淨
- Redis driver fail-open：cache outages degrade to misses, not request failures
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import pickle
import threading
from typing import Any, Protocol
from urllib.parse import quote

from cachetools import TTLCache
from redis.exceptions import RedisError

from utils.fail_open import capture_fail_open

logger = logging.getLogger(__name__)


class Cache(Protocol):
    """Cache driver 介面。所有方法同步、不拋例外（fail-open）。"""

    def get(self, namespace: str, key: str) -> Any | None: ...

    def set(self, namespace: str, key: str, value: Any, ttl: int) -> None: ...

    def delete(self, namespace: str, key: str) -> None: ...

    def clear_namespace(self, namespace: str) -> int: ...


# Memory driver 每 namespace 一個 TTLCache，避免 namespace 大小互相牽動 LRU eviction。
# default_maxsize=1024 對既有 callsite（多為 maxsize <= 512）夠用。
_DEFAULT_NAMESPACE_MAXSIZE = 1024


class MemoryCache:
    """In-process cache，內部每 namespace 一個 cachetools.TTLCache。"""

    def __init__(self, default_maxsize: int = _DEFAULT_NAMESPACE_MAXSIZE) -> None:
        self._default_maxsize = default_maxsize
        self._stores: dict[str, TTLCache] = {}
        self._lock = threading.Lock()
        # cachetools.TTLCache 文件明示「not thread-safe」（內部 linked-list 維護）。
        # FastAPI sync route 經 run_in_threadpool 可能並發進入，所以 get/set/delete/
        # clear_namespace 都在 self._lock 內保護。鎖開銷 µs 級，對 cache 路徑可吃。

    def _get_store(self, namespace: str, *, ttl_hint: int | None = None) -> TTLCache:
        """取出 namespace 對應的 TTLCache；不存在則 lazy 建立。

        TTLCache 的 ttl 是 per-store 的常數，set() 時無法 override 個別 entry 的 ttl。
        本實作採 **first-write-wins**：第一次 set 用 ttl_hint 建 store，之後同 namespace
        的 set 一律忽略傳入的 ttl，沿用 store 建立時的 ttl。

        Why first-write-wins: 11 個 PR1 callsite 每個各擁有獨立 namespace 且使用固定
        TTL 常數，混用不同 ttl 的需求不存在。若未來有需求，請用不同 namespace 分離。
        """
        # caller 已持 self._lock；不再二次鎖
        store = self._stores.get(namespace)
        if store is None:
            ttl = ttl_hint if ttl_hint is not None else 60
            store = TTLCache(maxsize=self._default_maxsize, ttl=ttl)
            self._stores[namespace] = store
        return store

    def get(self, namespace: str, key: str) -> Any | None:
        with self._lock:
            store = self._stores.get(namespace)
            if store is None:
                return None
            return store.get(key)

    def set(self, namespace: str, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            store = self._get_store(namespace, ttl_hint=ttl)
            store[key] = value

    def delete(self, namespace: str, key: str) -> None:
        with self._lock:
            store = self._stores.get(namespace)
            if store is None:
                return
            store.pop(key, None)

    def clear_namespace(self, namespace: str) -> int:
        with self._lock:
            store = self._stores.pop(namespace, None)
            if store is None:
                return 0
            size = len(store)
            store.clear()
            return size


_MAC_LEN = hashlib.sha256().digest_size  # 32 bytes，簽章 prefix 長度


class RedisCache:
    """Redis-backed cache driver.

    All operations fail open: cache outages should degrade to cache misses, not
    block business requests. Values are pickled because existing call sites cache
    Python objects, not only JSON-compatible payloads.

    安全（CWE-502 縱深防御）：`pickle.loads` 對不可信 bytes 等同 RCE。當提供
    `hmac_key`（prod 由 `jwt_secret_key` 注入）時，set 會在 payload 前綴
    HMAC-SHA256，get 先 `compare_digest` 驗章，未通過一律當 miss、絕不 unpickle。
    即使 Redis 被寫入惡意 pickle 也無法升級為 RCE。`hmac_key` 為空時退化為原本
    無簽章行為（dev/單機 memory 路徑不受影響）。切換簽章開關時舊 entry 驗章失敗
    → 自然當 miss 由 cache 重建，無需手動清理。
    """

    def __init__(
        self,
        *,
        redis_url: str,
        key_prefix: str = "ivy",
        hmac_key: bytes | str | None = None,
        socket_timeout: float = 5.0,
    ) -> None:
        import redis

        if not redis_url:
            raise RuntimeError("CACHE_REDIS_URL is required when CACHE_BACKEND=redis")
        # socket timeout 是 fail-open 的前提：本 driver 的 except 只擋「錯誤」，擋不住
        # 「hang」。同步 cache 在 request 熱路徑上，Redis 網路分割／established-but-hung
        # 連線若無 timeout 會卡死 thread 直到 OS TCP 逾時 → threadpool 耗盡。帶上
        # socket_timeout / socket_connect_timeout 讓 hang 在 timeout 後變成可被 except
        # 接住的例外、降級為 cache miss；retry_on_timeout=False 則確保逾時即放棄、
        # 不再 retry 拖長阻塞。
        self._client = redis.Redis.from_url(
            redis_url,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_timeout,
            retry_on_timeout=False,
        )
        self._prefix = f"{key_prefix}:cache:"
        if isinstance(hmac_key, str):
            hmac_key = hmac_key.encode()
        # 空 key 視同未設定，避免「弱/可預測簽章」的假安全感
        self._hmac_key: bytes | None = hmac_key or None

    def _key(self, namespace: str, key: str) -> str:
        ns = quote(namespace, safe="")
        k = quote(key, safe="")
        return f"{self._prefix}{ns}:{k}"

    def _namespace_pattern(self, namespace: str) -> str:
        ns = quote(namespace, safe="")
        return f"{self._prefix}{ns}:*"

    def _sign(self, payload: bytes) -> bytes:
        if self._hmac_key is None:
            return payload
        mac = hmac.new(self._hmac_key, payload, hashlib.sha256).digest()
        return mac + payload

    def _unwrap(self, raw: bytes) -> bytes | None:
        """剝離並驗證 HMAC prefix；驗章失敗回 None（caller 當 miss，不 unpickle）。"""
        if self._hmac_key is None:
            return raw
        if len(raw) < _MAC_LEN:
            return None
        mac, payload = raw[:_MAC_LEN], raw[_MAC_LEN:]
        expected = hmac.new(self._hmac_key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            return None
        return payload

    def get(self, namespace: str, key: str) -> Any | None:
        try:
            raw = self._client.get(self._key(namespace, key))
            if raw is None:
                return None
            payload = self._unwrap(raw)
            if payload is None:
                # 驗章失敗：當 miss。debug 級避免 key rotation 時噪音灌爆 Sentry。
                logger.debug(
                    "cache.redis.get rejected unverified payload ns=%s", namespace
                )
                return None
            return pickle.loads(payload)
        except Exception as exc:  # noqa: BLE001 - cache must fail open
            capture_fail_open("cache.redis.get", exc, namespace=namespace, key=key)
            return None

    def set(self, namespace: str, key: str, value: Any, ttl: int) -> None:
        try:
            payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            self._client.set(
                self._key(namespace, key), self._sign(payload), ex=max(int(ttl), 1)
            )
        except Exception as exc:  # noqa: BLE001 - cache must fail open
            capture_fail_open("cache.redis.set", exc, namespace=namespace, key=key)

    def delete(self, namespace: str, key: str) -> None:
        try:
            self._client.delete(self._key(namespace, key))
        except Exception as exc:  # noqa: BLE001 - cache must fail open
            capture_fail_open("cache.redis.delete", exc, namespace=namespace, key=key)

    def clear_namespace(self, namespace: str) -> int:
        try:
            keys = list(
                self._client.scan_iter(match=self._namespace_pattern(namespace))
            )
            if not keys:
                return 0
            try:
                self._client.unlink(*keys)
            except RedisError:
                self._client.delete(*keys)
            return len(keys)
        except Exception as exc:  # noqa: BLE001 - cache must fail open
            capture_fail_open("cache.redis.clear_namespace", exc, namespace=namespace)
            return 0


# ── Singleton ────────────────────────────────────────────────────────────

_cache_singleton: Cache | None = None
_singleton_lock = threading.Lock()


def get_cache() -> Cache:
    """取得（或 lazy 建立）cache singleton。

    依 `settings.cache.backend` 決定 driver。
    """
    global _cache_singleton
    if _cache_singleton is not None:
        return _cache_singleton
    with _singleton_lock:
        if _cache_singleton is not None:
            return _cache_singleton
        _cache_singleton = _build_cache()
    return _cache_singleton


def _build_cache() -> Cache:
    """依 settings 建 driver。"""
    from config import settings  # lazy 避免 import cycle

    backend = settings.cache.backend
    if backend == "memory":
        return MemoryCache()
    if backend == "redis":
        # prod 以 jwt_secret_key 簽章 Redis payload（CWE-502 縱深防御）；
        # 該 secret 在 redis backend（多 worker prod）必設，缺則退化為無簽章。
        return RedisCache(
            redis_url=settings.cache.redis_url or "",
            key_prefix=settings.cache.key_prefix,
            hmac_key=settings.core.jwt_secret_key,
            socket_timeout=settings.cache.pubsub_timeout_seconds,
        )
    raise ValueError(f"Unknown cache backend: {backend}")


def reset_cache_for_testing() -> None:
    """重置 singleton — 僅供測試使用。

    呼叫後下次 `get_cache()` 會重新建立。
    """
    global _cache_singleton
    with _singleton_lock:
        _cache_singleton = None
