# Cache Layer 集中化設計（in-process → Redis-ready）

- **日期**：2026-05-21
- **作者**：Claude（與 user brainstorming 對齊）
- **狀態**：Draft（待 user 簽核 → writing-plans）
- **影響範圍**：ivy-backend 12 個 cache callsite、ivy-frontend `useCachedAsync` + axios 攔截器、Zeabur Redis infra
- **PR 拆分**：3 個依序上的 PR（互不阻擋）

---

## 1. 背景與動機

`ivy-backend` 目前有 **11 個 in-process TTLCache** 散落在 8 個 module-level + 1 個 service 內 3 個 instance-level：

```
api/shifts.py                     _shift_type_cache (TTL 300)
api/attendance/upload.py          _shift_type_cache (TTL 300)
api/portal/_shared.py             _shift_type_cache (TTL 300)
api/config/__init__.py            _cache (TTL 300, 5 keys)
api/salary/detail.py              _snapshot_cache (TTL 600, lock 保護)
api/parent_portal/home.py         _home_summary_cache (TTL 60)
api/parent_portal/family.py       _timeline_cache (TTL 30)
services/dashboard_query_service  _notification_cache / _approval_cache / _events_cache
api/salary/__init__.py            from cachetools import TTLCache  (unused legacy import)
```

問題：
1. **無統一 invalidate API**：各檔自己寫 `_clear_cache()` / `_cache.pop()` / `_cache.clear()`，invalidate broadcast 沒有單一管道
2. **多 worker / 多 instance 部署即失效**：每個 process 自己一份；未來 scale 出第二個 replica 立刻面臨一致性 drift
3. **無 driver 抽象**：未來要切 Redis 必須 11 處同時改
4. **沒有跟 `utils/etag.py` 協同**：HTTP 304 命中時 server 還會重算 payload，因為兩層 cache 各管各的

`utils/rate_limit.py` 也在 `CLAUDE.md` 註記同病但語意不同（counter 非 KV），**本次不動**。

---

## 2. 目標與非目標

### 目標（A+B+C 全收）

- **A — Facade refactor**：抽 `utils/cache_layer.py`，11 個 callsite 統一 API；driver interface 預留 Redis 插槽
- **B — Redis driver**：Zeabur Redis service add-on 開通；`utils/cache_layer.py` 加 `RedisCache`；env flag 切換；fail-open
- **C — 前端 If-None-Match**：`src/composables/useCachedAsync.ts` + axios 攔截器自動帶 `If-None-Match`，304 時只 bump `fetchedAt` 不重新 parse；既有 callsite 零改動

### 非目標（明確 out of scope）

| Out of scope | 理由 |
|---|---|
| `services/report_cache_service.py` | 已是 DB-backed 跨 worker 一致，介面成熟 |
| `utils/finance_cache.py` | 上者的薄薄包裝，留在原位 |
| `utils/etag.py` | HTTP 層計算 ETag，分層清楚不融合 |
| `utils/rate_limit.py` | 語意是 counter，未來另一個 PR |
| Multi-worker invalidate broadcast (Redis pub/sub) | prod 仍 single worker；待真出問題再加 |
| Cache warming / pre-fetch | 不做 |
| CAS / 分散式鎖 / quorum | 不做 |
| Hit-rate / latency histogram metric | 無 Prometheus/OTLP 系統；純 log 抽樣足夠 |

---

## 3. 介面設計

```python
# utils/cache_layer.py
from typing import Any, Protocol

class Cache(Protocol):
    def get(self, namespace: str, key: str) -> Any | None: ...
    def set(self, namespace: str, key: str, value: Any, ttl: int) -> None: ...
    def delete(self, namespace: str, key: str) -> None: ...
    def clear_namespace(self, namespace: str) -> int: ...   # 回傳清掉幾筆


class MemoryCache:
    """每 namespace 一個 cachetools.TTLCache；lazy 建。"""

class RedisCache:
    """包 redis-py 同步 client，pool 共用。
    Wire key: f"{prefix}:{namespace}:v{SCHEMA_VERSION}:{key}"
      - SCHEMA_VERSION 初始 = 1，未來 pickle 不相容時手動 bump，
        舊 key 自動「冷處理」直到 TTL 過期（不需手動 purge）
    Value: pickle + zlib
    socket_timeout: 0.2s（200ms per call；prod LAN 預期 <5ms，
        超過此值寧可 fail-open 回 DB 也不阻塞 endpoint）
    Fail-open: 任何 RedisError / OSError / PickleError → log + 視為 miss
    """

def get_cache() -> Cache:
    """singleton；首次呼叫依 settings.cache.backend 建立。"""

def reset_cache_for_testing() -> None: ...
```

### 關鍵取捨

1. **同步 API**（非 async）：Redis LAN call 1-3ms 可接受；避免把 11 個 sync callsite 全部傳染成 async
2. **Namespace 為一級概念**：`clear_namespace("shift_type")` 一行清乾淨，不需 callsite 各自維護 `_clear_*` helper
3. **Pickle+zlib 序列化**：保留 ORM / dict / Pydantic shape；wire key 內嵌 `schema_version`，反序列化失敗即 fail-open
4. **Fail-open 內建 driver**：callsite 不需 try/except
5. **無 atomic CAS / pub-sub / lock**：scope 外，未來如需 broadcast 再加 `subscribe_invalidate()` API

### Callsite 範本

```python
from utils.cache_layer import get_cache

CACHE_NS = "shift_type"  # scope: global
CACHE_TTL = 300

def _get_all_shift_types_cached(session):
    cached = get_cache().get(CACHE_NS, "all")
    if cached is not None:
        return cached
    result = session.query(...).all()
    get_cache().set(CACHE_NS, "all", result, ttl=CACHE_TTL)
    return result

def _clear_shift_type_cache():
    get_cache().clear_namespace(CACHE_NS)
```

每個 callsite 在 namespace 常數旁邊用註解標 `# scope: <admin|user|global>`，reviewer 看一眼能 catch cache 語意問題。

---

## 4. Namespace 一覽（PR1 完工後 PR description 列）

| Namespace | 來源 | scope | TTL |
|---|---|---|---|
| `shift_type` | `api/shifts.py` | global | 300 |
| `attendance_shift_type` | `api/attendance/upload.py` | global | 300 |
| `portal_shift_type` | `api/portal/_shared.py` | global | 300 |
| `config_titles` / `config_attendance_policy` / `config_insurance_rates` / `config_deduction_types` / `config_bonus_types` | `api/config/__init__.py` | global | 300 |
| `parent_home_summary` | `api/parent_portal/home.py` | user | 60 |
| `parent_family_timeline` | `api/parent_portal/family.py` | user | 30 |
| `salary_snapshot` | `api/salary/detail.py` | global | 600 |
| `dashboard_notification_<school_id>` / `dashboard_approval_<school_id>` / `dashboard_events_<school_id>` | `services/dashboard_query_service.py` | global（per school） | 各原 TTL |

> Reviewer：grep 全表確認無重名；後續加新 namespace 前先 `grep "CACHE_NS = " api/ services/`。

---

## 5. 三階段 Migration

### PR1 — Facade refactor + 11 callsite migrate（memory driver only）

`ivy-backend`：
1. 新增 `utils/cache_layer.py`：Cache protocol + MemoryCache + `get_cache()` + `reset_cache_for_testing()`
2. 新增 `config/cache_settings.py` 並接 `Settings`：`backend: Literal["memory","redis"] = "memory"`、`redis_url: str | None`、`key_prefix: str = "ivy"`
3. 改造 12 個 callsite（11 個真實 + 1 個 unused import 刪除）
4. 新增 `tests/test_cache_layer.py`
5. 既有 cache 相關 pytest 不改

**完工 acceptance**：`pytest` 全綠、callsite 行為等價、`grep "from cachetools import" api/ services/` 為空。

**Commit 切分（控制風險）**：5-7 個 commit，每 1-2 個 callsite 一 commit。dashboard_query_service 的 3 個 instance-level cache 一個 commit。

### PR2 — Zeabur Redis 開通 + Redis driver + env flag

`ivy-backend`：
1. `requirements.txt` 加 `redis>=5.0`、`fakeredis>=2.20` (dev/test only)
2. `utils/cache_layer.py` 補 `RedisCache`
3. `get_cache()` 依 `settings.cache.backend` 切換
4. 新增 `tests/test_cache_layer_redis.py`（用 `fakeredis`，CI 不依賴真 Redis）
5. 新增 `docs/cache_layer_runbook.md`：Zeabur Redis 開通步驟 + env vars + Sentry watch 條件

**Infra 作業（user 在 PR2 merge 前親手做）：**
- Zeabur dashboard → add Redis service
- 設 `REDIS_URL` / `CACHE_BACKEND=redis` 到 backend service 的環境變數
- Sentry 建 alert rule：`message:"cache_layer.redis_error" count > 5/min` → email/Slack

**完工 acceptance**：`CACHE_BACKEND=memory` 與 `CACHE_BACKEND=redis` 兩條測試路徑都綠；prod 切到 redis 後手動觀測 Sentry 內 `cache_layer.redis_error` 為 0（一週內），確認 driver 健康。

### PR3 — 前端 useCachedAsync + axios If-None-Match

`ivy-frontend`：
1. `src/api/index.ts` augment AxiosResponse 加 `_etag`；response interceptor 把 `response.headers.etag` 寫入；request interceptor 自動帶 `If-None-Match`（讀 module-level `_etagStore`）
2. `src/composables/useCachedAsync.ts`：`CacheEntry` 加 `etag: string | null`；200 → 存 etag；304 → 只 bump `fetchedAt`
3. 既有 callsite **零改動**（auto-enroll）
4. 新增 `tests/composables/useCachedAsync.spec.ts` ETag flow 測試 + `tests/api/etag-interceptor.spec.ts`

**完工 acceptance**：DevTools network panel 上掛 `etag_response` 的 endpoint 第二次 fetch 看到 304；vitest 全綠；`schema.d.ts` 不需 regen。

---

## 6. 錯誤處理與觀測

### Fail-open（driver 內建）

```python
class RedisCache:
    def get(self, namespace, key):
        try:
            raw = self._client.get(self._wire_key(namespace, key))
            return self._deserialize(raw) if raw else None
        except (RedisError, OSError, pickle.UnpicklingError) as exc:
            logger.warning(
                "cache_layer.redis_error op=get ns=%s key=%s err=%s",
                namespace, key, exc.__class__.__name__,
            )
            return None
```

`set` / `delete` / `clear_namespace` 同樣 fail-open + log。

### Log key 一覽

| Log key | 出現情境 | 預期頻率 |
|---|---|---|
| `cache_layer.redis_error op=<get\|set\|delete\|clear> ns=<ns> err=<class>` | Redis 暫時斷線 / 序列化失敗 | ≤ 5/hr 正常；爆量 = infra 警示 |
| `cache_layer.deserialize_version_mismatch ns=<ns>` | wire key 內 schema_version 不符 | 升級期可見；穩定後 0 |

不做：hit/miss counter、latency histogram。

### Sentry 整合（PR2 範圍）

複用既有 `utils/sentry_init.py` logger handler，`logger.warning("cache_layer.redis_error ...")` 自動成為 breadcrumb（不升 issue 但有 context）。

**Alert rule（在 Sentry 後台手動建，寫入 runbook）**：
- query：`message:"cache_layer.redis_error"`
- threshold：count > 5/min
- channel：email/Slack

### Fail-open 的可觀測代價

Redis 壞掉首先看到的不是 endpoint 5xx，而是 **DB 負載飆高 + Sentry warning rate 漲**。Runbook 寫明「Zeabur Redis service 健康狀態需納入 oncall watch」，不能只靠 Sentry alert 晚一拍。

### PII 風險評估

後端 cache 內容已過 RLS / permission 檢查；cache key 內嵌 user_id 隔離 user-scope 資料（如 `parent_home_summary` key=`user_id`）。唯一風險是 callsite bug 把 admin cache 放進 user namespace；對策：每個 callsite 在 namespace 常數註解 `# scope: <admin|user|global>`，reviewer 看這條判斷。

---

## 7. 測試策略

| Layer | 測試檔 | 涵蓋 |
|---|---|---|
| Cache layer 本身 | `tests/test_cache_layer.py` (PR1) | MemoryCache `get/set/delete/clear_namespace`、namespace 隔離、TTL 過期、singleton、`reset_for_testing`、`get_cache()` 依 env 切換 |
| Redis driver | `tests/test_cache_layer_redis.py` (PR2) | `fakeredis` mock、wire key format、pickle round-trip、fail-open（mock 拋 `RedisError`）、clear_namespace SCAN+UNLINK |
| Callsite migration | 既有 pytest (PR1) | shifts / config / parent_portal/home / family 等既有測試不改，等價驗證 |
| 前端 ETag flow | `tests/composables/useCachedAsync.spec.ts` (PR3) + `tests/api/etag-interceptor.spec.ts` | 200 → 存 etag；304 → bump fetchedAt 不改 data；axios interceptor 自動帶 If-None-Match |

**明確不做**：
- Redis 真連線整合測試（CI 全用 fakeredis）
- Hit/miss rate metric assertion
- Multi-worker invalidate broadcast 測試

---

## 8. 風險與迴避

| 風險 | 對策 |
|---|---|
| 12 callsite 一次改炸某條 path 測試漏 | PR1 切 5-7 commit，每 1-2 callsite 一 commit，逐步可 revert |
| `dashboard_query_service.py` 3 個 cache 含 `school_id` 等 instance state | 改成 namespace = `dashboard_<purpose>_<school_id>`，把 instance state 編進 namespace |
| Pickle 跨 Python / 套件版本不穩 | Redis wire key 內嵌 `SCHEMA_VERSION`（初始 1），反序列化失敗 fail-open；升級套件後若需強制清空就 bump SCHEMA_VERSION（舊 key 隨 TTL 自然死亡） |
| `RedisCache.clear_namespace` 用 SCAN + UNLINK 在 namespace 變大時延遲高 | 單一 namespace key 數量 << 1000（每 namespace 都是 enum 或小集合）；若未來某 namespace 爆量再加 keyspace prefix sharding |
| Namespace collision | PR1 PR description 列全表 + CLAUDE.md 加「新增前先 grep 既有 namespace」 |
| 與當前兩個 worktree（`jwt-secret-rotation` / `scheduler-leader-election`）撞檔 | 兩 worktree 不動 `utils/cache_layer.py`（新檔）與 12 個 callsite 檔；起 worktree 前再次 grep 確認 |

---

## 9. 後續展開（本 spec 範圍外）

- `utils/rate_limit.py` 改 Redis-backed counter（不同語意但可共用 Redis 連線）
- Redis pub/sub broadcast invalidate（多 worker 上線後再加）
- `utils/etag.py` 結合 cache_layer 達成 304 命中時 skip DB query（需另外設計 cache key↔ETag 對應）

---

## 10. 簽核

- 設計 brainstorming：2026-05-21
- User 確認：A+B+C 全包 / Zeabur Redis / 三階段 PR / Fail-open
- 下一步：writing-plans → 產生 PR1 實作計畫
