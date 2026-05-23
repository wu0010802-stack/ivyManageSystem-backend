"""utils/cache_layer.py — 統一 in-process cache facade。

Why: 此前各 router/service 各自 `from cachetools import TTLCache` 建私有
cache，invalidate API 不一致、未來無法 atomic 切 Redis backend。本檔
提供 driver-agnostic 介面：

- `get_cache()` — singleton；依 `settings.cache.backend` 選擇 driver
- `MemoryCache` — PR1 唯一 driver，包 cachetools.TTLCache
- `RedisCache` — PR2 加入
- `reset_cache_for_testing()` — 測試用，清掉 singleton 與所有 namespace

關鍵設計：
- 同步 API（非 async），避免 11 個 sync callsite 傳染成 async
- Namespace 為一級概念：`clear_namespace(ns)` 一行清乾淨
- Driver fail-open 內建（PR1 memory driver 不會失敗；PR2 Redis driver 才有）
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

from cachetools import TTLCache

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


# ── Singleton ────────────────────────────────────────────────────────────

_cache_singleton: Cache | None = None
_singleton_lock = threading.Lock()


def get_cache() -> Cache:
    """取得（或 lazy 建立）cache singleton。

    依 `settings.cache.backend` 決定 driver（PR1 只有 memory；PR2 加入 redis）。
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
    """依 settings 建 driver；PR1 一律回 MemoryCache，未來 PR2 加 redis 分支。"""
    from config import settings  # lazy 避免 import cycle

    backend = settings.cache.backend
    if backend == "memory":
        return MemoryCache()
    if backend == "redis":
        # PR2 才實作；保留分支讓 settings validation 不會掉到「未知 backend」
        raise NotImplementedError("Redis backend will be added in PR2")
    raise ValueError(f"Unknown cache backend: {backend}")


def reset_cache_for_testing() -> None:
    """重置 singleton — 僅供測試使用。

    呼叫後下次 `get_cache()` 會重新建立。
    """
    global _cache_singleton
    with _singleton_lock:
        _cache_singleton = None
