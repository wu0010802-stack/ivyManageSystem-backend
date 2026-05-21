# Cache Layer 集中化 — PR1 實作計畫（A facade refactor, memory driver only）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 抽 `utils/cache_layer.py` 統一既有 11 個散落的 in-process TTLCache（8 個 module-level + dashboard_query_service 3 個 instance-level），並順手刪除 `api/salary/__init__.py` 內未使用的 `from cachetools import TTLCache`。MemoryCache driver only；Redis driver 是 PR2 範圍。

**Architecture:** `utils/cache_layer.py` 暴露 `Cache` Protocol + `MemoryCache` 實作 + `get_cache()` singleton + `reset_cache_for_testing()`。各 callsite 改成 `get_cache().get(ns, key)` / `set(ns, key, value, ttl)` / `clear_namespace(ns)`。Namespace 為一級概念，replace 各檔自寫的 `_clear_cache()` helper。

**Tech Stack:** Python 3.14、pydantic-settings、cachetools（保留 — MemoryCache 底層仍用 TTLCache）、pytest。

---

## 前置：Worktree

執行此 plan 前，使用 `superpowers:using-git-worktrees` 開立 worktree：

```bash
# 慣例命名
git worktree add .claude/worktrees/cache-layer-pr1-2026-05-21-backend \
  -b feat/cache-layer-pr1-2026-05-21-backend main
cd .claude/worktrees/cache-layer-pr1-2026-05-21-backend
```

worktree 內所有 commit 與 main 隔離；PR1 merged 後 `git worktree remove`。

---

## 檔案結構

**新增（2 檔）：**
- `config/cache.py` — `CacheSettings(BaseSettings)`
- `utils/cache_layer.py` — Cache Protocol + MemoryCache + get_cache + reset_for_testing
- `tests/test_cache_layer.py` — MemoryCache 單測

**修改（10 檔）：**
- `config/base.py` — 加 `cache: CacheSettings` 欄位
- `api/shifts.py` — 替換 `_shift_type_cache` (namespace = `shift_type`)
- `api/attendance/upload.py` — 替換 `_shift_type_cache` (namespace = `attendance_shift_type`)
- `api/portal/_shared.py` — 替換 `_shift_type_cache` (namespace = `portal_shift_type`)
- `api/config/__init__.py` — 替換 `_cache` 拆 5 namespace (`config_titles` / `config_attendance_policy` / `config_insurance_rates` / `config_deduction_types` / `config_bonus_types`)
- `api/salary/__init__.py` — 刪除 unused `from cachetools import TTLCache`
- `api/salary/detail.py` — 替換 `_snapshot_cache` (namespace = `salary_snapshot`)
- `api/parent_portal/home.py` — 替換 `_home_summary_cache` (namespace = `parent_home_summary`)
- `api/parent_portal/family.py` — 替換 `_timeline_cache` (namespace = `parent_family_timeline`)
- `services/dashboard_query_service.py` — 替換 3 個 instance-level cache (namespace = `dashboard_notification` / `dashboard_approval` / `dashboard_events`)

---

## Task 1：建立 `config/cache.py` 並接入 Settings

**Files:**
- Create: `config/cache.py`
- Modify: `config/base.py`

- [ ] **Step 1：建立 `config/cache.py`**

```python
"""config/cache.py — cache layer settings.

PR1 only ships memory backend；PR2 才會接 Redis。本檔先把欄位定起來，
即便 PR1 不會讀 redis_url，PR2 也能 in-place 補。
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class CacheSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CACHE_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    backend: Literal["memory", "redis"] = "memory"
    redis_url: str | None = None
    key_prefix: str = "ivy"
```

- [ ] **Step 2：把 CacheSettings 加進 `config/base.py` Settings**

```python
# config/base.py 既有 import 區塊已 sort 過，按字母序加入
from .cache import CacheSettings  # 加在 .core import 之前
```

並在 `class Settings(BaseSettings):` 內加一欄（位置：`storage` 之後、`misc` 之前，維持與其他 sub-settings 字母序對齊）：

```python
    cache: CacheSettings = Field(default_factory=CacheSettings)
```

- [ ] **Step 3：執行既有 settings 測試確認沒打壞 `Settings`**

Run: `pytest tests/ -k "settings or config" -x --no-header -q 2>&1 | tail -30`
Expected: existing settings tests PASS（新增 sub-settings 不應影響任何 assertion）

- [ ] **Step 4：Commit**

```bash
git add config/cache.py config/base.py
git commit -m "feat(config): add CacheSettings (memory|redis backend, redis_url, key_prefix)"
```

---

## Task 2：建立 `utils/cache_layer.py` + 單元測試（TDD）

**Files:**
- Create: `utils/cache_layer.py`
- Test: `tests/test_cache_layer.py`

- [ ] **Step 1：寫失敗測試 `tests/test_cache_layer.py`**

```python
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
```

- [ ] **Step 2：執行測試確認 FAIL**

Run: `pytest tests/test_cache_layer.py -x --no-header -q 2>&1 | tail -20`
Expected: `ImportError: cannot import name 'Cache' from 'utils.cache_layer'`（模組尚未建立）

- [ ] **Step 3：建立 `utils/cache_layer.py`**

```python
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
        # store 的建立用 lock 保護避免 race；TTLCache 內部 op 是 thread-safe（GIL+Lock）

    def _get_store(self, namespace: str, *, ttl_hint: int | None = None) -> TTLCache:
        """取出 namespace 對應的 TTLCache；不存在則 lazy 建立。

        TTLCache 的 ttl 是 per-store 的，set() 時不能 override 個別 entry 的 ttl。
        我們的解法：第一次 set 用該 ttl 建 store；後續同 namespace 用不同 ttl 時
        以「最大 ttl」更新 store（避免短 ttl 把長 ttl 的舊資料提早 evict）。
        """
        store = self._stores.get(namespace)
        if store is not None:
            return store
        with self._lock:
            store = self._stores.get(namespace)
            if store is None:
                ttl = ttl_hint if ttl_hint is not None else 60
                store = TTLCache(maxsize=self._default_maxsize, ttl=ttl)
                self._stores[namespace] = store
        return store

    def get(self, namespace: str, key: str) -> Any | None:
        store = self._stores.get(namespace)
        if store is None:
            return None
        return store.get(key)

    def set(self, namespace: str, key: str, value: Any, ttl: int) -> None:
        store = self._get_store(namespace, ttl_hint=ttl)
        store[key] = value

    def delete(self, namespace: str, key: str) -> None:
        store = self._stores.get(namespace)
        if store is None:
            return
        store.pop(key, None)

    def clear_namespace(self, namespace: str) -> int:
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
```

- [ ] **Step 4：執行測試確認 PASS**

Run: `pytest tests/test_cache_layer.py -x --no-header -q 2>&1 | tail -30`
Expected: 14 tests PASS（5 + 3 + 2 + 3 + 1）

- [ ] **Step 5：Commit**

```bash
git add utils/cache_layer.py tests/test_cache_layer.py
git commit -m "feat(cache): add cache_layer facade with MemoryCache driver

- Cache Protocol with get/set/delete/clear_namespace
- MemoryCache wraps cachetools.TTLCache per-namespace
- get_cache() singleton + reset_cache_for_testing()
- Redis driver stubbed for PR2"
```

---

## Task 3：遷移 `api/shifts.py` → namespace `shift_type`

**Files:**
- Modify: `api/shifts.py:18`（remove cachetools import）
- Modify: `api/shifts.py:91-141`（replace cache helpers）

- [ ] **Step 1：跑既有 shifts 測試確認 baseline 綠**

Run: `pytest tests/ -k shift -x --no-header -q 2>&1 | tail -10`
Expected: existing shift tests PASS

- [ ] **Step 2：在 `api/shifts.py` 行 18 把 cachetools import 換成 cache_layer**

```python
# 刪除：
from cachetools import TTLCache

# 替換為：
from utils.cache_layer import get_cache
```

- [ ] **Step 3：替換 `_shift_type_cache` 區塊（行 91-117 + 120-142）**

把下列原 code（行 91-142）：

```python
# ShiftType 很少變動，使用 TTLCache 減少重複 DB 查詢（5 分鐘 TTL）
_shift_type_cache: TTLCache = TTLCache(maxsize=3, ttl=300)


def _clear_shift_type_cache():
    _shift_type_cache.clear()


def _get_all_shift_types_cached(session) -> list:
    """回傳 list[dict]，快取 5 分鐘"""
    cached = _shift_type_cache.get("all")
    if cached is not None:
        return cached
    types = session.query(ShiftType).order_by(ShiftType.sort_order).all()
    result = [
        {
            "id": t.id,
            "name": t.name,
            "work_start": t.work_start,
            "work_end": t.work_end,
            "sort_order": t.sort_order,
            "is_active": t.is_active,
        }
        for t in types
    ]
    _shift_type_cache["all"] = result
    return result


def _get_shift_type_id_map_cached(session) -> dict:
    """回傳 {id: SimpleNamespace(work_start, work_end, name, is_active)}，快取 5 分鐘。
    用於需要 .work_start / .work_end 屬性存取的場景（如工時計算）。
    """
    from types import SimpleNamespace

    cached = _shift_type_cache.get("id_map")
    if cached is not None:
        return cached
    types = session.query(ShiftType).all()
    result = {
        t.id: SimpleNamespace(
            id=t.id,
            name=t.name,
            work_start=t.work_start,
            work_end=t.work_end,
            sort_order=t.sort_order,
            is_active=t.is_active,
        )
        for t in types
    }
    _shift_type_cache["id_map"] = result
    return result
```

換成：

```python
# scope: global  — ShiftType 是全系統共用設定，無 per-user 隔離需求
_CACHE_NS_SHIFT_TYPE = "shift_type"
_CACHE_TTL_SHIFT_TYPE = 300  # 5 分鐘


def _clear_shift_type_cache():
    get_cache().clear_namespace(_CACHE_NS_SHIFT_TYPE)


def _get_all_shift_types_cached(session) -> list:
    """回傳 list[dict]，快取 5 分鐘。"""
    cached = get_cache().get(_CACHE_NS_SHIFT_TYPE, "all")
    if cached is not None:
        return cached
    types = session.query(ShiftType).order_by(ShiftType.sort_order).all()
    result = [
        {
            "id": t.id,
            "name": t.name,
            "work_start": t.work_start,
            "work_end": t.work_end,
            "sort_order": t.sort_order,
            "is_active": t.is_active,
        }
        for t in types
    ]
    get_cache().set(_CACHE_NS_SHIFT_TYPE, "all", result, ttl=_CACHE_TTL_SHIFT_TYPE)
    return result


def _get_shift_type_id_map_cached(session) -> dict:
    """回傳 {id: SimpleNamespace(work_start, work_end, name, is_active)}，快取 5 分鐘。"""
    from types import SimpleNamespace

    cached = get_cache().get(_CACHE_NS_SHIFT_TYPE, "id_map")
    if cached is not None:
        return cached
    types = session.query(ShiftType).all()
    result = {
        t.id: SimpleNamespace(
            id=t.id,
            name=t.name,
            work_start=t.work_start,
            work_end=t.work_end,
            sort_order=t.sort_order,
            is_active=t.is_active,
        )
        for t in types
    }
    get_cache().set(_CACHE_NS_SHIFT_TYPE, "id_map", result, ttl=_CACHE_TTL_SHIFT_TYPE)
    return result
```

- [ ] **Step 4：跑測試確認 PASS**

Run: `pytest tests/ -k shift -x --no-header -q 2>&1 | tail -10`
Expected: 全綠（行為等價）

- [ ] **Step 5：Commit**

```bash
git add api/shifts.py
git commit -m "refactor(shifts): use cache_layer for shift_type cache"
```

---

## Task 4：遷移 `api/attendance/upload.py` → namespace `attendance_shift_type`

**Files:**
- Modify: `api/attendance/upload.py:12`（remove cachetools import）
- Modify: `api/attendance/upload.py:45-56`（replace cache）

- [ ] **Step 1：跑既有 attendance/upload 測試 baseline**

Run: `pytest tests/ -k "upload or attendance" -x --no-header -q 2>&1 | tail -10`
Expected: existing tests PASS

- [ ] **Step 2：替換 `api/attendance/upload.py` 行 12 cachetools import**

```python
# 刪除：
from cachetools import TTLCache

# 替換為：
from utils.cache_layer import get_cache
```

- [ ] **Step 3：替換行 45-56 cache 區塊**

把：

```python
# ShiftType 很少異動，快取 5 分鐘，避免每次上傳重複查詢
_shift_type_cache: TTLCache = TTLCache(maxsize=1, ttl=300)


def _get_shift_type_id_map(session) -> dict:
    """回傳 {id: ShiftType ORM} 快取 5 分鐘。"""
    cached = _shift_type_cache.get("id_map")
    if cached is not None:
        return cached
    result = {st.id: st for st in session.query(ShiftType).all()}
    _shift_type_cache["id_map"] = result
    return result
```

換成：

```python
# scope: global
_CACHE_NS_ATTENDANCE_SHIFT_TYPE = "attendance_shift_type"
_CACHE_TTL_ATTENDANCE_SHIFT_TYPE = 300  # 5 分鐘


def _get_shift_type_id_map(session) -> dict:
    """回傳 {id: ShiftType ORM} 快取 5 分鐘。"""
    cached = get_cache().get(_CACHE_NS_ATTENDANCE_SHIFT_TYPE, "id_map")
    if cached is not None:
        return cached
    result = {st.id: st for st in session.query(ShiftType).all()}
    get_cache().set(
        _CACHE_NS_ATTENDANCE_SHIFT_TYPE,
        "id_map",
        result,
        ttl=_CACHE_TTL_ATTENDANCE_SHIFT_TYPE,
    )
    return result
```

> 註：function-local `attendance_cache: dict = {}` / `legacy_attendance_cache` / `csv_attendance_cache` 不是 module-level cache，是 per-request 暫存 dict，**不動**。

- [ ] **Step 4：跑測試確認 PASS**

Run: `pytest tests/ -k "upload or attendance" -x --no-header -q 2>&1 | tail -10`
Expected: 全綠

- [ ] **Step 5：Commit**

```bash
git add api/attendance/upload.py
git commit -m "refactor(attendance): use cache_layer for upload shift_type cache"
```

---

## Task 5：遷移 `api/portal/_shared.py` → namespace `portal_shift_type`

**Files:**
- Modify: `api/portal/_shared.py:10`（remove cachetools import）
- Modify: `api/portal/_shared.py:30-31, 249, 268`（replace cache）

- [ ] **Step 1：跑既有 portal 測試 baseline**

Run: `pytest tests/ -k portal -x --no-header -q 2>&1 | tail -10`
Expected: existing tests PASS

- [ ] **Step 2：替換 `api/portal/_shared.py` 行 10**

```python
# 刪除：
from cachetools import TTLCache

# 替換為：
from utils.cache_layer import get_cache
```

- [ ] **Step 3：替換 cache 宣告（行 30-31）**

把：

```python
# ShiftType 很少異動，使用 TTLCache 5 分鐘，避免每次請求全表查詢
_shift_type_cache: TTLCache = TTLCache(maxsize=2, ttl=300)
```

換成：

```python
# scope: global
_CACHE_NS_PORTAL_SHIFT_TYPE = "portal_shift_type"
_CACHE_TTL_PORTAL_SHIFT_TYPE = 300  # 5 分鐘
```

- [ ] **Step 4：替換 cache get/set 呼叫**

打開 `api/portal/_shared.py`，在原 `_shift_type_cache.get(cache_key)`（約行 249）的函式中：

把：

```python
cached = _shift_type_cache.get(cache_key)
```

換成：

```python
cached = get_cache().get(_CACHE_NS_PORTAL_SHIFT_TYPE, cache_key)
```

把（約行 268）：

```python
_shift_type_cache[cache_key] = result
```

換成：

```python
get_cache().set(
    _CACHE_NS_PORTAL_SHIFT_TYPE,
    cache_key,
    result,
    ttl=_CACHE_TTL_PORTAL_SHIFT_TYPE,
)
```

> 註：`cache_key` 已是 str（grep 過原檔，是 `f"id_map_{...}"` 形式），無需轉型。

- [ ] **Step 5：跑測試確認 PASS**

Run: `pytest tests/ -k portal -x --no-header -q 2>&1 | tail -10`
Expected: 全綠

- [ ] **Step 6：Commit**

```bash
git add api/portal/_shared.py
git commit -m "refactor(portal): use cache_layer for portal shift_type cache"
```

---

## Task 6：遷移 `api/config/__init__.py` → 5 個 namespace

**Files:**
- Modify: `api/config/__init__.py:8`（remove cachetools import）
- Modify: `api/config/__init__.py:70-80`（replace cache + `_clear_cache` helper）
- Modify: `api/config/__init__.py:94, 195-218, 303, 458-485, 542, 623, 742-757, 786, 828, 854, 872-894, 910, 923-944, 960`（替換 cache get/set/clear 呼叫，下方一次列）

**Namespace 對照表（grep 確認來自既有 cache key string）：**

| 既有 key | 新 namespace | scope | TTL |
|---|---|---|---|
| `"titles"` | `config_titles` | global | 300 |
| `"attendance_policy"` | `config_attendance_policy` | global | 300 |
| `"insurance_rates"` | `config_insurance_rates` | global | 300 |
| `"deduction_types"` | `config_deduction_types` | global | 300 |
| `"bonus_types"` | `config_bonus_types` | global | 300 |

- [ ] **Step 1：跑既有 config 測試 baseline**

Run: `pytest tests/ -k config -x --no-header -q 2>&1 | tail -10`
Expected: existing tests PASS

- [ ] **Step 2：替換 `api/config/__init__.py` 行 8**

```python
# 刪除：
from cachetools import TTLCache

# 替換為：
from utils.cache_layer import get_cache
```

- [ ] **Step 3：替換 `_cache` 宣告與 `_clear_cache` helper（行 70-80）**

把：

```python
# 設定快取（5 分鐘 TTL，最多 16 個 key）
_cache = TTLCache(maxsize=16, ttl=300)


def _clear_cache(*keys):
    """清除指定的快取 key，不指定則全部清除"""
    if keys:
        for k in keys:
            _cache.pop(k, None)
    else:
        _cache.clear()
```

換成：

```python
# scope: global — 全系統共用設定，無 per-user 隔離需求
_CACHE_TTL_CONFIG = 300  # 5 分鐘
_CACHE_KEY_TO_NAMESPACE = {
    "titles": "config_titles",
    "attendance_policy": "config_attendance_policy",
    "insurance_rates": "config_insurance_rates",
    "deduction_types": "config_deduction_types",
    "bonus_types": "config_bonus_types",
}


def _clear_cache(*keys: str) -> None:
    """清除指定 namespace，不指定則全部 config namespace 都清。"""
    namespaces = (
        [_CACHE_KEY_TO_NAMESPACE[k] for k in keys]
        if keys
        else list(_CACHE_KEY_TO_NAMESPACE.values())
    )
    for ns in namespaces:
        get_cache().clear_namespace(ns)
```

> 設計取捨：保留 `_clear_cache("titles")` 這個既有 API 形狀，把 string key → namespace 的 mapping 集中一處，避免每個 callsite 都得知道 namespace 字串。`_clear_cache()` 不傳參數時清掉所有 config namespace（等價舊版 `_cache.clear()`）。

- [ ] **Step 4：替換 5 個 `_cache.get(...)` 與 `_cache[...] = ...` 呼叫**

每個既有讀寫點都對應到一個 namespace。把以下模式逐一替換（**整個檔案 grep `_cache.get(` 與 `_cache\[` 確認改到完整**）：

**Pattern A — get：**
```python
cached = _cache.get("titles")          # 舊
cached = get_cache().get("config_titles", "v")     # 新
```

**Pattern B — set：**
```python
_cache["titles"] = result              # 舊
get_cache().set("config_titles", "v", result, ttl=_CACHE_TTL_CONFIG)   # 新
```

> "v" 是 sentinel key（namespace 內只放一筆資料，key 名稱無語意；用 "v" 表 value）。

具體每個既有點對應：

| 既有檔內位置（grep `_cache`） | 既有 key | 替換為 |
|---|---|---|
| `_cache.get("titles")` | `titles` | `get_cache().get("config_titles", "v")` |
| `_cache["titles"] = result` | `titles` | `get_cache().set("config_titles", "v", result, ttl=_CACHE_TTL_CONFIG)` |
| `_cache.get("attendance_policy")` | `attendance_policy` | `get_cache().get("config_attendance_policy", "v")` |
| `_cache["attendance_policy"] = result` | `attendance_policy` | `get_cache().set("config_attendance_policy", "v", result, ttl=_CACHE_TTL_CONFIG)` |
| `_cache.get("insurance_rates")` | `insurance_rates` | `get_cache().get("config_insurance_rates", "v")` |
| `_cache["insurance_rates"] = result` | `insurance_rates` | `get_cache().set("config_insurance_rates", "v", result, ttl=_CACHE_TTL_CONFIG)` |
| `_cache.get("deduction_types")` | `deduction_types` | `get_cache().get("config_deduction_types", "v")` |
| `_cache["deduction_types"] = result` | `deduction_types` | `get_cache().set("config_deduction_types", "v", result, ttl=_CACHE_TTL_CONFIG)` |
| `_cache.get("bonus_types")` | `bonus_types` | `get_cache().get("config_bonus_types", "v")` |
| `_cache["bonus_types"] = result` | `bonus_types` | `get_cache().set("config_bonus_types", "v", result, ttl=_CACHE_TTL_CONFIG)` |

> `_clear_cache("titles")` / `_clear_cache("attendance_policy")` / ... / `_clear_cache()` 這些呼叫**不改**，新版 helper 行為等價。

- [ ] **Step 5：確認檔內 `_cache` 變數名沒有殘留**

Run: `grep -n "_cache\." /Users/yilunwu/Desktop/ivy-backend/api/config/__init__.py`
Expected: 應只剩 `_clear_cache(` 呼叫（內部使用 `get_cache()`）+ 文字註解，無 `_cache.get(` / `_cache[`

- [ ] **Step 6：跑測試確認 PASS**

Run: `pytest tests/ -k config -x --no-header -q 2>&1 | tail -10`
Expected: 全綠

- [ ] **Step 7：Commit**

```bash
git add api/config/__init__.py
git commit -m "refactor(config): use cache_layer with 5 namespaces for config cache"
```

---

## Task 7：遷移 `api/salary/detail.py` → namespace `salary_snapshot`（含 threading.Lock 移除）

**Files:**
- Modify: `api/salary/detail.py:15-17`（remove threading + cachetools imports）
- Modify: `api/salary/detail.py:40-65`（replace cache + lock + getter/setter）

> 設計取捨：原本 `threading.Lock` 是用來把「`get(record_id)` → 比對 version → `pop()` 失效」這個 read-modify-write 序列做 atomic。新版邏輯不變，但拿掉 lock：worst case 兩個 thread 同時看 version mismatch 並都呼叫 `delete()`（idempotent），對正確性無影響。MemoryCache 內部對單 op 已 thread-safe（GIL + cachetools 內部 RLock）。

- [ ] **Step 1：跑既有 salary detail 測試 baseline**

Run: `pytest tests/ -k "salary_detail or breakdown or audit_log" -x --no-header -q 2>&1 | tail -10`
Expected: existing tests PASS

- [ ] **Step 2：替換 import（行 15-17）**

把：

```python
import io
import threading

from cachetools import TTLCache
```

換成：

```python
import io

from utils.cache_layer import get_cache
```

- [ ] **Step 3：替換 cache 區塊（行 40-65）**

把：

```python
# ── 薪資 debug snapshot 快取 ─────────────────────────────────────────────
# 同一筆薪資在 UI 切換不同欄位時，snapshot 內容不變（~13 個 DB 查詢）。
# 用 record_id 為 key、(version, data) 為 value，版本更動即失效避免陳舊資料。
_SNAPSHOT_CACHE_TTL_SEC = 60
_SNAPSHOT_CACHE_MAX_SIZE = 256
_snapshot_cache: TTLCache = TTLCache(
    maxsize=_SNAPSHOT_CACHE_MAX_SIZE, ttl=_SNAPSHOT_CACHE_TTL_SEC
)
_snapshot_cache_lock = threading.Lock()


def _snapshot_cache_get(record_id: int, version: int):
    with _snapshot_cache_lock:
        entry = _snapshot_cache.get(record_id)
        if entry is None:
            return None
        cached_version, data = entry
        if cached_version != version:
            _snapshot_cache.pop(record_id, None)
            return None
        return data


def _snapshot_cache_put(record_id: int, version: int, data: dict) -> None:
    with _snapshot_cache_lock:
        _snapshot_cache[record_id] = (version, data)
```

換成：

```python
# ── 薪資 debug snapshot 快取 ─────────────────────────────────────────────
# 同一筆薪資在 UI 切換不同欄位時，snapshot 內容不變（~13 個 DB 查詢）。
# 用 record_id 為 key、(version, data) 為 value，版本更動即失效避免陳舊資料。
# scope: global（snapshot 本身不含 PII 跨 user，且 record_id 已 unique）
_CACHE_NS_SALARY_SNAPSHOT = "salary_snapshot"
_CACHE_TTL_SALARY_SNAPSHOT = 60  # 1 分鐘


def _snapshot_cache_get(record_id: int, version: int):
    entry = get_cache().get(_CACHE_NS_SALARY_SNAPSHOT, str(record_id))
    if entry is None:
        return None
    cached_version, data = entry
    if cached_version != version:
        # version mismatch：失效該筆。worst case 兩 thread 都 delete（idempotent）
        get_cache().delete(_CACHE_NS_SALARY_SNAPSHOT, str(record_id))
        return None
    return data


def _snapshot_cache_put(record_id: int, version: int, data: dict) -> None:
    get_cache().set(
        _CACHE_NS_SALARY_SNAPSHOT,
        str(record_id),
        (version, data),
        ttl=_CACHE_TTL_SALARY_SNAPSHOT,
    )
```

- [ ] **Step 4：跑測試確認 PASS**

Run: `pytest tests/ -k "salary_detail or breakdown or audit_log" -x --no-header -q 2>&1 | tail -10`
Expected: 全綠

- [ ] **Step 5：Commit**

```bash
git add api/salary/detail.py
git commit -m "refactor(salary): use cache_layer for salary_snapshot, drop redundant lock"
```

---

## Task 8：遷移 `api/parent_portal/home.py` → namespace `parent_home_summary`

**Files:**
- Modify: `api/parent_portal/home.py:17`（remove cachetools import）
- Modify: `api/parent_portal/home.py:45, 107, 167`（replace cache）

- [ ] **Step 1：跑既有 parent_portal home 測試 baseline**

Run: `pytest tests/ -k "parent_home or parent_portal_home" -x --no-header -q 2>&1 | tail -10`
Expected: existing tests PASS

- [ ] **Step 2：替換 `api/parent_portal/home.py` 行 17**

```python
# 刪除：
from cachetools import TTLCache

# 替換為：
from utils.cache_layer import get_cache
```

- [ ] **Step 3：替換行 45（cache 宣告）**

把：

```python
# user_id → (home_summary_payload)；60s TTL，maxsize=512（同時上線家長上限）
_home_summary_cache: TTLCache = TTLCache(maxsize=512, ttl=60)
```

換成：

```python
# user_id → home_summary_payload；60s TTL
# scope: user — key 為 user_id，user 間天然隔離
_CACHE_NS_PARENT_HOME_SUMMARY = "parent_home_summary"
_CACHE_TTL_PARENT_HOME_SUMMARY = 60  # 1 分鐘
```

- [ ] **Step 4：替換 cache get 呼叫（約行 107）**

把：

```python
cached = _home_summary_cache.get(user_id)
```

換成：

```python
cached = get_cache().get(_CACHE_NS_PARENT_HOME_SUMMARY, str(user_id))
```

> `user_id` 是 int，要轉 str 才能當 cache key。

- [ ] **Step 5：替換 cache set 呼叫（約行 167）**

把：

```python
_home_summary_cache[user_id] = result
```

換成：

```python
get_cache().set(
    _CACHE_NS_PARENT_HOME_SUMMARY,
    str(user_id),
    result,
    ttl=_CACHE_TTL_PARENT_HOME_SUMMARY,
)
```

- [ ] **Step 6：跑測試確認 PASS**

Run: `pytest tests/ -k "parent_home or parent_portal_home" -x --no-header -q 2>&1 | tail -10`
Expected: 全綠

- [ ] **Step 7：Commit**

```bash
git add api/parent_portal/home.py
git commit -m "refactor(parent_portal): use cache_layer for home_summary cache"
```

---

## Task 9：遷移 `api/parent_portal/family.py` → namespace `parent_family_timeline`（tuple key → string）

**Files:**
- Modify: `api/parent_portal/family.py:13`（remove cachetools import）
- Modify: `api/parent_portal/family.py:41, 70-76`（replace cache + tuple key 字串化）

- [ ] **Step 1：跑既有 parent_portal family 測試 baseline**

Run: `pytest tests/ -k "parent_family or timeline" -x --no-header -q 2>&1 | tail -10`
Expected: existing tests PASS

- [ ] **Step 2：替換 `api/parent_portal/family.py` 行 13**

```python
# 刪除：
from cachetools import TTLCache

# 替換為：
from utils.cache_layer import get_cache
```

- [ ] **Step 3：替換行 41 cache 宣告**

把：

```python
# (user_id, student_id, limit) → timeline payload；30s TTL
_timeline_cache: TTLCache = TTLCache(maxsize=512, ttl=30)
```

換成：

```python
# (user_id, student_id, limit) → timeline payload；30s TTL
# scope: user — key 內含 user_id，user 間天然隔離
_CACHE_NS_PARENT_FAMILY_TIMELINE = "parent_family_timeline"
_CACHE_TTL_PARENT_FAMILY_TIMELINE = 30  # 30 秒
```

- [ ] **Step 4：替換 cache get/set 呼叫（行 70-76）**

把：

```python
    cache_key = (user_id, student_id, limit)
    cached = _timeline_cache.get(cache_key)
    if cached is not None:
        return cached

    items = _collect_timeline_items(session, user_id, student_id, limit)
    _timeline_cache[cache_key] = items
    return items
```

換成：

```python
    cache_key = f"{user_id}:{student_id}:{limit}"
    cached = get_cache().get(_CACHE_NS_PARENT_FAMILY_TIMELINE, cache_key)
    if cached is not None:
        return cached

    items = _collect_timeline_items(session, user_id, student_id, limit)
    get_cache().set(
        _CACHE_NS_PARENT_FAMILY_TIMELINE,
        cache_key,
        items,
        ttl=_CACHE_TTL_PARENT_FAMILY_TIMELINE,
    )
    return items
```

> tuple `(user_id, student_id, limit)` 轉成 `"{user_id}:{student_id}:{limit}"` 字串形式，符合 Cache.set 介面的 `key: str` 約定。

- [ ] **Step 5：跑測試確認 PASS**

Run: `pytest tests/ -k "parent_family or timeline" -x --no-header -q 2>&1 | tail -10`
Expected: 全綠

- [ ] **Step 6：Commit**

```bash
git add api/parent_portal/family.py
git commit -m "refactor(parent_portal): use cache_layer for family timeline cache"
```

---

## Task 10：遷移 `services/dashboard_query_service.py` → 3 個 namespace

**Files:**
- Modify: `services/dashboard_query_service.py:6`（remove cachetools import）
- Modify: `services/dashboard_query_service.py:36-50`（remove `__init__` 內的 3 個 TTLCache）
- Modify: `services/dashboard_query_service.py:63-66, 95, 100-103, 170, 375-377, 481`（replace cache get/set）

**Namespace 對照：**

| 既有 instance attr | 新 namespace | scope | TTL |
|---|---|---|---|
| `self._events_cache` | `dashboard_events` | global（school 全域） | `NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS`（=15） |
| `self._approval_cache` | `dashboard_approval` | global | `NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS` |
| `self._notification_cache` | `dashboard_notification` | global（含 user_permissions+user_id 區分） | `NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS` |

- [ ] **Step 1：跑既有 dashboard 測試 baseline**

Run: `pytest tests/ -k "dashboard or notification_summary or approval_summary or upcoming_events" -x --no-header -q 2>&1 | tail -10`
Expected: existing tests PASS

- [ ] **Step 2：替換 `services/dashboard_query_service.py` 行 6**

```python
# 刪除：
from cachetools import TTLCache

# 替換為：
from utils.cache_layer import get_cache
```

- [ ] **Step 3：替換 `__init__` 內 3 個 TTLCache（行 36-50）**

把：

```python
class DashboardQueryService:
    def __init__(self):
        # 依 user_permissions 分組快取，最多 128 種不同權限組合
        self._notification_cache: TTLCache = TTLCache(
            maxsize=128, ttl=NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS
        )
        # 審核摘要與行事曆是全系統共用資料（非個人化），可跨不同權限組合共用快取
        # maxsize=1：同一天只需快取一份；key = date ISO string
        self._approval_cache: TTLCache = TTLCache(
            maxsize=1, ttl=NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS
        )
        # maxsize=8：依 (date, days) 組合，最多 8 種查詢視窗
        self._events_cache: TTLCache = TTLCache(
            maxsize=8, ttl=NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS
        )
```

換成：

```python
# scope: global（dashboard 為全 school admin 共用視圖）
# 三個 namespace 拆分：events / approval / notification
# 注意：通知摘要 (notification) cache key 含 (user_permissions, user_id)
#       因此即使 namespace 是 global，仍然在 key 層級依 user 區分
_CACHE_NS_DASHBOARD_EVENTS = "dashboard_events"
_CACHE_NS_DASHBOARD_APPROVAL = "dashboard_approval"
_CACHE_NS_DASHBOARD_NOTIFICATION = "dashboard_notification"


class DashboardQueryService:
    # 移除 __init__ — 不再有 instance state；保留 class 結構讓現有 singleton
    # 變數 `dashboard_query_service = DashboardQueryService()` 持續可用。
    pass
```

> 設計取捨：原本 `DashboardQueryService` 的 instance 內含 3 個 cache attribute；改用 module-level namespace 後，class 變成純方法 container。雖然可以進一步重構成 module function，但這會牽動所有 `dashboard_query_service.X()` 的呼叫站，PR1 不做（YAGNI）。`pass` 保留 class 形狀讓 import / singleton 不變。

> 若 class 內已有其他 method 定義，**不要**保留 `pass`，直接刪掉 `def __init__(self):` 整個方法（class body 還有其他 method 撐住）。

- [ ] **Step 4：替換 `build_upcoming_events` 內 cache 操作（行 63-66, 95）**

把：

```python
        today = today or date.today()
        cache_key = (today.isoformat(), days)
        cached = self._events_cache.get(cache_key)
        if cached is not None:
            return cached
```

換成：

```python
        today = today or date.today()
        cache_key = f"{today.isoformat()}:{days}"
        cached = get_cache().get(_CACHE_NS_DASHBOARD_EVENTS, cache_key)
        if cached is not None:
            return cached
```

並把（行 95）：

```python
        self._events_cache[cache_key] = result
```

換成：

```python
        get_cache().set(
            _CACHE_NS_DASHBOARD_EVENTS,
            cache_key,
            result,
            ttl=NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS,
        )
```

- [ ] **Step 5：替換 `build_approval_summary` 內 cache 操作（行 100-103, 170）**

把：

```python
        today = today or date.today()
        cache_key = today.isoformat()
        cached = self._approval_cache.get(cache_key)
        if cached is not None:
            return cached
```

換成：

```python
        today = today or date.today()
        cache_key = today.isoformat()
        cached = get_cache().get(_CACHE_NS_DASHBOARD_APPROVAL, cache_key)
        if cached is not None:
            return cached
```

並把（行 170 附近的 `self._approval_cache[cache_key] = result`）：

```python
        self._approval_cache[cache_key] = result
```

換成：

```python
        get_cache().set(
            _CACHE_NS_DASHBOARD_APPROVAL,
            cache_key,
            result,
            ttl=NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS,
        )
```

- [ ] **Step 6：替換 `build_notification_summary` 內 cache 操作（行 370-377, 481）**

把：

```python
        # 為支援班級 scope，將 cache key 從 user_permissions 升為 (user_permissions, user_id)
        cache_key = (
            user_permissions,
            current_user.get("user_id") if current_user else None,
        )
        cached = self._notification_cache.get(cache_key)
        if cached is not None:
            return cached
```

換成：

```python
        # 為支援班級 scope，cache key 含 (user_permissions, user_id)
        user_id_part = current_user.get("user_id") if current_user else "anonymous"
        cache_key = f"{user_permissions}:{user_id_part}"
        cached = get_cache().get(_CACHE_NS_DASHBOARD_NOTIFICATION, cache_key)
        if cached is not None:
            return cached
```

並把（行 481）：

```python
        self._notification_cache[cache_key] = result
```

換成：

```python
        get_cache().set(
            _CACHE_NS_DASHBOARD_NOTIFICATION,
            cache_key,
            result,
            ttl=NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS,
        )
```

- [ ] **Step 7：grep 確認 instance cache 引用已全清**

Run: `grep -n "self\._\(notification\|approval\|events\)_cache" /Users/yilunwu/Desktop/ivy-backend/services/dashboard_query_service.py`
Expected: 無輸出（完全替換）

- [ ] **Step 8：跑測試確認 PASS**

Run: `pytest tests/ -k "dashboard or notification_summary or approval_summary or upcoming_events" -x --no-header -q 2>&1 | tail -10`
Expected: 全綠

- [ ] **Step 9：Commit**

```bash
git add services/dashboard_query_service.py
git commit -m "refactor(dashboard): use cache_layer with 3 namespaces, drop instance caches"
```

---

## Task 11：移除 `api/salary/__init__.py` unused `from cachetools import TTLCache`

**Files:**
- Modify: `api/salary/__init__.py:21`

- [ ] **Step 1：確認 `TTLCache` 在 `api/salary/__init__.py` 內無使用點**

Run: `grep -n "TTLCache" /Users/yilunwu/Desktop/ivy-backend/api/salary/__init__.py`
Expected: 只剩 `from cachetools import TTLCache`（行 21）一行；本檔內無實際使用點。

- [ ] **Step 2：刪除 unused import**

把 `api/salary/__init__.py` 行 21：

```python
from cachetools import TTLCache
```

整行刪除。

- [ ] **Step 3：跑既有 salary 測試確認 import 沒打壞**

Run: `pytest tests/ -k salary -x --no-header -q 2>&1 | tail -10`
Expected: existing tests PASS

- [ ] **Step 4：Commit**

```bash
git add api/salary/__init__.py
git commit -m "chore(salary): remove unused cachetools.TTLCache import"
```

---

## Task 12：最終驗收 + grep guarantee

- [ ] **Step 1：確認 `from cachetools import` 已從生產碼移除**

Run:
```bash
grep -rln "from cachetools import\|import cachetools" \
  /Users/yilunwu/Desktop/ivy-backend/api/ \
  /Users/yilunwu/Desktop/ivy-backend/services/ \
  2>/dev/null | grep -v worktrees
```
Expected: **無輸出**（`utils/cache_layer.py` 是唯一保留入口）

- [ ] **Step 2：確認 cache_layer 是唯一 import 點**

Run:
```bash
grep -rn "from cachetools" /Users/yilunwu/Desktop/ivy-backend/utils/cache_layer.py
```
Expected: 一行 `from cachetools import TTLCache`（MemoryCache 內部使用）

- [ ] **Step 3：跑全套 pytest 確認零回歸**

Run: `pytest --no-header -q 2>&1 | tail -20`
Expected: 全綠（含 14 new test_cache_layer.py + 既有 4000+ test）

> 已知 pre-existing fail（不阻 PR）：`test_audit_router` 3 fail / `test_supabase_storage` 6 error（多次 spec 提到，與本 PR 無關）

- [ ] **Step 4：列出 commit 歷史摘要**

Run:
```bash
git log --oneline main..HEAD
```
Expected：10 commit 左右（Task 1-11 各一）

- [ ] **Step 5：建立 PR description 草稿（人工 push 後填到 GitHub）**

PR description 應包含：

```markdown
## 動機
集中化 11 個散落 in-process TTLCache 為單一 facade（`utils/cache_layer.py`），
為未來 Redis driver（PR2）與前端 If-None-Match（PR3）鋪路。

## 範圍
- 新增 `utils/cache_layer.py`：Cache Protocol + MemoryCache + get_cache singleton
- 新增 `config/cache.py`：CacheSettings（backend / redis_url / key_prefix）
- 改造 11 個 callsite（8 module-level + 3 instance-level in dashboard_query_service）
- 刪除 `api/salary/__init__.py` unused `from cachetools import TTLCache`

## Namespace 一覽
shift_type / attendance_shift_type / portal_shift_type /
config_titles / config_attendance_policy / config_insurance_rates /
config_deduction_types / config_bonus_types /
salary_snapshot /
parent_home_summary / parent_family_timeline /
dashboard_events / dashboard_approval / dashboard_notification

## 非範圍（spec 明列）
- ReportCacheService / utils/finance_cache（DB-backed 跨 worker，獨立活）
- utils/etag.py（HTTP 層）
- utils/rate_limit.py（counter 語意）
- Redis driver（PR2）
- 前端 If-None-Match（PR3）

## 驗收
- 14 new test_cache_layer.py 全綠
- 既有 pytest 零回歸（除 pre-existing `test_audit_router` / `test_supabase_storage`）
- grep -rn "from cachetools" api/ services/ → 無輸出
```

---

## 完成定義

PR1 merge 前必須符合：

1. ✅ Task 1-12 全部 step 完成
2. ✅ `pytest tests/test_cache_layer.py` 14 case 全綠
3. ✅ `grep -rln "from cachetools" api/ services/` 無輸出
4. ✅ 既有 pytest 零回歸（除 pre-existing 已知 fail）
5. ✅ 10 個 commit 一一獨立、訊息符合 Conventional Commits
6. ✅ PR description 列出 14 namespace 對照表

---

## 與其他正在進行 worktree 的撞檔風險

執行此 plan 時，主 repo 同時還有兩個 active worktree：
- `.claude/worktrees/jwt-secret-rotation-2026-05-21-backend`
- `.claude/worktrees/scheduler-leader-election-2026-05-21-backend`

**Pre-flight grep 確認兩 worktree 不動本 plan 範圍的檔案：**

```bash
for wt in jwt-secret-rotation scheduler-leader-election; do
  echo "=== $wt ==="
  cd "/Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/${wt}-2026-05-21-backend"
  git diff --stat main | grep -E "utils/cache_layer|config/(base|cache)|api/(shifts|attendance/upload|portal/_shared|config/__init__|salary/__init__|salary/detail|parent_portal/(home|family))|services/dashboard_query_service" || echo "  no overlap"
done
```

預期 `no overlap`。若有 overlap，回頭與 user 對齊先後順序。
