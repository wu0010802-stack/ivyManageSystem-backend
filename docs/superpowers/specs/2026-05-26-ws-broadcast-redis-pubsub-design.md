# WebSocket 廣播 Redis Pub/Sub 跨 instance fanout 設計（2026-05-26）

## 1. 背景與動機

### 1.1 問題現況

兩個 WebSocket 模組的 ConnectionManager 都在 **process memory**，多 instance 部署時跨 instance broadcast 會失效：

- `api/dismissal_ws.py:43-108` — `DismissalConnectionManager` 直接持有 `dict[int, list[WebSocket]]`（teacher）+ `list[WebSocket]`（admin），全在當前 process
- `api/contact_book_ws.py:33` — `hub = ChannelHub()`（來自 `utils/ws_hub.py`），同 process-local subscribe/unsubscribe 模式
- `zbpack.json:3` — 啟動指令 `uvicorn main:app --host 0.0.0.0 --port $PORT`，無 `--workers`、無 gunicorn

也就是說：**目前單 instance 跑得起來**，但 Zeabur 橫向 scale 到 2+ instance 後 — 家長 / 教師連到 instance A、events 從 instance B publish → 收不到任何事件。**現在是「scale 前必修」狀態。**

### 1.2 既存解法盤點（避免重複造輪）

| 議題 | 現況 | 結論 |
|------|------|------|
| Scheduler leader election | `utils/advisory_lock.try_scheduler_lock` 已實作 PG `pg_try_advisory_xact_lock`；`main.py:246` 已套用 activity_waitlist_sweep | ✅ 已解，本 spec 不動 |
| Rate limit 跨 worker | `utils/rate_limit.PostgresLimiter` 已實作（`rate_limit_buckets` 表 + UPSERT 原子計數），`RATE_LIMIT_BACKEND=postgres` 切換 | ✅ 已解，本 spec 不動 |
| Cache layer | `config/cache.py` 已預留 `backend: Literal["memory", "redis"]` + `redis_url` 欄位（檔案自註「PR1 only ships memory backend；PR2 才會接 Redis」） | 🚧 預埋；本 spec 順手接上 Redis client，cache 切換是 follow-up |
| WS broadcast 跨 instance | **無解** | ❌ 本 spec 處理 |

### 1.3 phase-1/2a 依賴關係

`feat/notification-dispatch-phase-1-2026-05-25-backend` 與 `feat/notification-dispatch-phase-2a-approvals-2026-05-25-backend` 兩 worktree 正在新增 WS broadcast 接點：

- `api/inbox_ws.py:31` — `await hub.broadcast([INBOX_USER_KEY(user_id)], payload)`
- `services/notification/_channels/ws.py:53,58` — `broadcast_parent(...)` + `dismissal_manager.broadcast(...)`

也就是說：**現在繼續 merge phase-1/2a 等於把 in-memory broadcast 問題複製到更多 WS channel**。順序決定為 **Redis broadcast 先 merge，phase-1/2a 在之上 rebase**，避免技術債累積。

### 1.4 不在本 spec 範圍

- Redis-backed cache layer（`config/cache.py` 的 PR2 work — 本 spec 引入 Redis client 但不動 cache 邏輯）
- Redis-backed rate limit（`PostgresLimiter` 已堪用，未來性能瓶頸再升級）
- WS protocol 變動（前端不需配合修改）
- 訊息可靠性升級到 at-least-once（沿用 best-effort，與現況一致）

## 2. 設計總覽

### 2.1 收斂目標

把 4 個現有 WS broadcast 點（dismissal teacher / dismissal admin / contact_book classroom / contact_book parent）收斂為 **單一 `BroadcastBackend` singleton**，透過 ENV `CACHE_BACKEND` 在 `LocalBackend`（記憶體）與 `RedisBackend`（Redis Pub/Sub）之間切換。

```
┌────────────────────────────────────────────────────────────────────────┐
│  Caller (router / service)                                              │
│  ──────────────────────────────                                         │
│    backend = get_broadcast()                                            │
│    await backend.publish("dismissal.admin", payload)                    │
│    await backend.publish_many([f"dismissal.classroom.{cid}",            │
│                                "dismissal.admin"], payload)             │
└────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────────┐
│  BroadcastBackend (abstract)                                            │
│  ──────────────────────────────                                         │
│    abstract publish(channel, payload) -> async                          │
│    abstract subscribe(channel, ws) -> sync                              │
│    abstract unsubscribe(ws) -> sync                                     │
│    abstract start() / stop() -> async (lifespan hooks)                  │
└────────────────────────────────────────────────────────────────────────┘
            │                                          │
            ▼                                          ▼
┌──────────────────────────────┐    ┌──────────────────────────────────────┐
│  LocalBackend                 │    │  RedisBackend                         │
│  ──────────────────────────── │    │  ──────────────────────────────────── │
│  _subscribers: dict[ch, ws]   │    │  _subscribers: dict[ch, ws]           │
│                                │    │  _redis: aioredis.Redis (publish)     │
│  publish:                     │    │  _pubsub: PubSub                      │
│    _local_dispatch(ch, data)  │    │  _pump_task: asyncio.Task            │
│                                │    │                                       │
│                                │    │  publish:                             │
│                                │    │    _local_dispatch(ch, data)          │
│                                │    │    await _redis.publish(prefix+ch,    │
│                                │    │                          json)        │
│                                │    │                                       │
│                                │    │  pump (long-running):                 │
│                                │    │    psubscribe "ivy:*"                 │
│                                │    │    async for msg in listen():         │
│                                │    │      _local_dispatch(strip(ch), data) │
└──────────────────────────────┘    └──────────────────────────────────────┘
```

### 2.2 關鍵設計決定

1. **同時走本地 + Redis**：`RedisBackend.publish` 永遠先 `_local_dispatch` 再 `redis.publish`。Redis 短暫掉線時同 instance 仍可收到事件（fail-open）；跨 instance 訊息透過 Redis `psubscribe` 流入再走 `_local_dispatch`。**不會重複** — 本地路徑只在 publish 當下執行，pump 路徑只處理流入訊息，兩條互斥。
2. **Pub/Sub 而非 Streams**：best-effort 語意符合現況（既有 WS 斷線重連本就會掉事件），來源都有持久化（`dismissal_call` / `contact_book_entry` / `notification_logs`），可透過 REST endpoint 重拉。Streams + consumer group 過度設計。
3. **Channel key 用字串**：原本 dismissal 用 `dict[int, list[WS]]`、contact_book 用 `tuple[str, int]`，本 spec 統一為 string key（`dismissal.classroom.{cid}` / `contact_book.parent.{uid}`），便於 Redis pattern subscribe `psubscribe "ivy:*"`。
4. **與 cache 共用 Redis settings**：`config/cache.py` 既有 `backend` / `redis_url` / `key_prefix`，broadcast 直接 reuse 同一份。**不引入 `BROADCAST_*` 新 env**。
5. **`get_broadcast()` 單例**：與 `get_settings()` lru_cache pattern 一致，main.py lifespan 啟動時 `await get_broadcast().start()`，shutdown 時 `await get_broadcast().stop()`。

## 3. 介面定義

### 3.1 `utils/broadcast/__init__.py`

```python
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
        """syntactic sugar — for ch in channels: await self.publish(ch, payload)"""
        for ch in channels:
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
    if settings.cache.backend == "redis":
        from utils.broadcast.redis import RedisBackend
        return RedisBackend(
            redis_url=settings.cache.redis_url,
            key_prefix=settings.cache.key_prefix,
            payload_max_bytes=settings.cache.publish_payload_max_bytes,
        )
    from utils.broadcast.local import LocalBackend
    return LocalBackend(payload_max_bytes=settings.cache.publish_payload_max_bytes)


def reset_for_tests() -> None:
    """test fixture 用 — 清掉 lru_cache 讓下次 get_broadcast() 重建。"""
    get_broadcast.cache_clear()
```

### 3.2 Channel 命名規範

```
dismissal.classroom.{cid}    — 取代 dismissal_ws._teacher_conns[cid]
dismissal.admin              — 取代 dismissal_ws._admin_conns
contact_book.classroom.{cid} — 取代 ChannelHub("classroom", cid) 教師端
contact_book.parent.{uid}    — 取代 ChannelHub("parent", uid) 家長端
inbox.user.{uid}             — phase-1 預留（inbox_ws）
```

- 全部小寫、`.` 分層、ID 後綴
- 加 prefix 後實際 Redis channel 是 `{key_prefix}:{channel}`（預設 `ivy:dismissal.admin`）
- `psubscribe "ivy:*"` 一次拿全部 broadcast

### 3.3 Payload schema

```python
# 不變動 — 與現有 broadcast 一致
payload: dict = {
    "type": str,           # event type 字串（"dismissal.created" / "contact_book.ack" / …）
    "data": dict,          # event-specific payload
    "ts": str,             # ISO timestamp（caller 填入）
    # 不可含 PII raw 欄位 — 仰賴既有 _scrub_event Sentry hook + 既有 line_service PII 規範
}
```

## 4. 元件詳細

### 4.1 `utils/broadcast/local.py`

```python
import asyncio
import json
import logging
from collections import defaultdict
from fastapi import WebSocket

from utils.broadcast import BroadcastBackend
from utils.ws_hub import MAX_BROADCAST_RETRIES, BROADCAST_RETRY_DELAY

logger = logging.getLogger(__name__)


class LocalBackend(BroadcastBackend):
    def __init__(self, *, payload_max_bytes: int = 8192):
        self._subscribers: dict[str, list[WebSocket]] = defaultdict(list)
        self._payload_max_bytes = payload_max_bytes

    async def publish(self, channel: str, payload: dict) -> None:
        await self._local_dispatch(channel, payload)

    def subscribe(self, channel: str, ws: WebSocket) -> None:
        self._subscribers[channel].append(ws)

    def unsubscribe(self, ws: WebSocket) -> None:
        for lst in self._subscribers.values():
            if ws in lst:
                lst.remove(ws)

    async def start(self) -> None:
        return  # no-op

    async def stop(self) -> None:
        return  # no-op

    async def _local_dispatch(self, channel: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str)
        if len(body.encode("utf-8")) > self._payload_max_bytes:
            raise ValueError(
                f"broadcast payload too large for channel={channel}: "
                f"{len(body)} bytes > {self._payload_max_bytes}"
            )
        targets = list(self._subscribers.get(channel, []))
        dead = []
        for ws in targets:
            sent = False
            for attempt in range(1, MAX_BROADCAST_RETRIES + 1):
                try:
                    await ws.send_text(body)
                    sent = True
                    break
                except Exception as exc:
                    if attempt < MAX_BROADCAST_RETRIES:
                        await asyncio.sleep(BROADCAST_RETRY_DELAY)
                    else:
                        logger.warning(
                            "broadcast send 失敗 channel=%s attempt=%d: %s",
                            channel, attempt, exc,
                        )
            if not sent:
                dead.append(ws)
        for ws in dead:
            self.unsubscribe(ws)
```

**等價驗證**：`_local_dispatch` 行為與既有 `DismissalConnectionManager.broadcast`（dismissal_ws.py:74-105）+ `ChannelHub.broadcast`（utils/ws_hub.py）邏輯等價（重試、sweep dead）。Local 模式上 prod 與現況 byte-identical。

### 4.2 `utils/broadcast/redis.py`

```python
import asyncio
import json
import logging
import time
from collections import defaultdict

import redis.asyncio as aioredis
from fastapi import WebSocket
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError

from utils.broadcast import BroadcastBackend
from utils.ws_hub import MAX_BROADCAST_RETRIES, BROADCAST_RETRY_DELAY

logger = logging.getLogger(__name__)

_SENTRY_THROTTLE_SECONDS = 60


class RedisBackend(BroadcastBackend):
    def __init__(self, *, redis_url: str, key_prefix: str, payload_max_bytes: int):
        if not redis_url:
            raise RuntimeError("CACHE_REDIS_URL is required when CACHE_BACKEND=redis")
        self._redis_url = redis_url
        self._prefix = f"{key_prefix}:"
        self._payload_max_bytes = payload_max_bytes
        self._subscribers: dict[str, list[WebSocket]] = defaultdict(list)
        self._redis: aioredis.Redis | None = None
        self._pubsub = None
        self._pump_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._last_sentry_ts: float = 0.0

    async def start(self) -> None:
        self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        self._pubsub = self._redis.pubsub()
        await self._pubsub.psubscribe(f"{self._prefix}*")
        self._pump_task = asyncio.create_task(self._pump(), name="broadcast-pump")
        logger.info("RedisBackend started prefix=%s", self._prefix)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._pump_task:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._pubsub:
            await self._pubsub.close()
        if self._redis:
            await self._redis.close()
        logger.info("RedisBackend stopped")

    async def publish(self, channel: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str)
        if len(body.encode("utf-8")) > self._payload_max_bytes:
            raise ValueError(
                f"broadcast payload too large for channel={channel}: "
                f"{len(body)} bytes > {self._payload_max_bytes}"
            )
        # 1) 永遠先本地（fail-open）
        await self._dispatch_local(channel, body)
        # 2) Redis fanout 跨 instance
        try:
            await self._redis.publish(self._prefix + channel, body)
        except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
            self._note_redis_failure("publish", exc)

    def subscribe(self, channel: str, ws: WebSocket) -> None:
        self._subscribers[channel].append(ws)

    def unsubscribe(self, ws: WebSocket) -> None:
        for lst in self._subscribers.values():
            if ws in lst:
                lst.remove(ws)

    async def _pump(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async for msg in self._pubsub.listen():
                    if msg.get("type") != "pmessage":
                        continue
                    channel = msg["channel"]
                    if channel.startswith(self._prefix):
                        channel = channel[len(self._prefix):]
                    await self._dispatch_local(channel, msg["data"])
                backoff = 1.0  # listen() 正常結束 → reset
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._note_redis_failure("pump", exc)
                await asyncio.sleep(min(backoff, 30))
                backoff *= 2
                try:
                    await self._pubsub.psubscribe(f"{self._prefix}*")
                except Exception:
                    continue

    async def _dispatch_local(self, channel: str, body: str) -> None:
        targets = list(self._subscribers.get(channel, []))
        dead = []
        for ws in targets:
            sent = False
            for attempt in range(1, MAX_BROADCAST_RETRIES + 1):
                try:
                    await ws.send_text(body)
                    sent = True
                    break
                except Exception as exc:
                    if attempt < MAX_BROADCAST_RETRIES:
                        await asyncio.sleep(BROADCAST_RETRY_DELAY)
                    else:
                        logger.warning(
                            "broadcast send 失敗 channel=%s attempt=%d: %s",
                            channel, attempt, exc,
                        )
            if not sent:
                dead.append(ws)
        for ws in dead:
            self.unsubscribe(ws)

    def _note_redis_failure(self, kind: str, exc: BaseException) -> None:
        now = time.monotonic()
        if now - self._last_sentry_ts < _SENTRY_THROTTLE_SECONDS:
            logger.warning("redis %s failed (throttled): %s", kind, exc)
            return
        self._last_sentry_ts = now
        logger.warning("redis %s failed: %s", kind, exc)
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass
```

**關鍵點**：

- `_dispatch_local` 同時供 `publish`（同 instance push）與 `_pump`（跨 instance push）使用 — **不會重複**，因為 publish 路徑只在本 instance 執行一次本地分派、Redis 流入路徑來自其他 instance 的 publish（不會 echo 自己 publish 的訊息嗎？實際上 Redis Pub/Sub 不 echo，subscriber 收不到自己 publish 的訊息）。
- 但**保險起見**：publish 端不送本地是另一種選擇 — 缺點是 Redis 慢時同 instance 也延遲。我們選「同時送 + 容忍 echo」是因為 Redis Pub/Sub native 不會 echo，實測為準。
- **疑慮**：`aioredis.PubSub.psubscribe` 可能在某些 client 版本會 echo（待 implementation 期 verify）。如果實測 echo，加 `publish_msg_id` 帶 instance UUID + dedupe set 解決。本 spec 先採「不會 echo」假設，implementation 階段驗證。
- `_note_redis_failure` 限頻：60s 內只 capture 一次 Sentry，避免 Redis 長期掛掉時噴爆 quota。

### 4.3 `main.py` lifespan 整合

```python
# main.py (既有 lifespan)
from utils.broadcast import get_broadcast

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... 既有 scheduler / dispatch hook init ...
    await get_broadcast().start()
    try:
        yield
    finally:
        await get_broadcast().stop()
        # ... 既有 scheduler shutdown ...
```

`get_broadcast()` lru_cache 保證 startup 跟所有 caller 拿到同一個 instance。

### 4.4 既有 WS 模組改造

#### `api/dismissal_ws.py`

**刪除**：`DismissalConnectionManager` class（43-108 行）+ `manager = ...` singleton
**新增**：

```python
from utils.broadcast import get_broadcast

# WS endpoint 內：
classroom_ids = _get_teacher_classroom_ids(employee_id)
backend = get_broadcast()
await ws.accept()
for cid in classroom_ids:
    backend.subscribe(f"dismissal.classroom.{cid}", ws)
await _run_connection(ws, cleanup=lambda: backend.unsubscribe(ws))

# admin endpoint 內：
backend.subscribe("dismissal.admin", ws)
await ws.accept()
await _run_connection(ws, cleanup=lambda: backend.unsubscribe(ws))
```

**caller 端**（`api/students.py:915, 1002, 1262` + `services/notification/_channels/ws.py:58`）：

```python
# 舊：
await dismissal_manager.broadcast(classroom_id, event)
# 新：
await get_broadcast().publish_many(
    [f"dismissal.classroom.{classroom_id}", "dismissal.admin"],
    event,
)
```

**保留**：`dismissal_manager` 名稱以 module-level alias 形式暫留一版（指向 deprecated wrapper），避免 phase-1/2a worktree rebase 時 `from api.dismissal_ws import dismissal_manager` 直接炸。alias 標 `DeprecationWarning`，下下版移除。

#### `api/contact_book_ws.py`

**刪除**：`hub = ChannelHub()` singleton + `TEACHER_CLASSROOM_KEY` / `PARENT_USER_KEY` lambda
**保留**：`broadcast_classroom(classroom_id, event)` / `broadcast_parent(parent_user_id, event)` 兩 helper（thin wrapper）：

```python
from utils.broadcast import get_broadcast

async def broadcast_classroom(classroom_id: int, event: dict) -> None:
    await get_broadcast().publish(f"contact_book.classroom.{classroom_id}", event)

async def broadcast_parent(parent_user_id: int, event: dict) -> None:
    await get_broadcast().publish(f"contact_book.parent.{parent_user_id}", event)
```

phase-1/2a worktree 內 `services/contact_book_service.py:121` / `_channels/ws.py:53` 不用改一行 — rebase 後跑既有 test 應該綠。

#### `utils/ws_hub.py`

**保留**：`run_ws_connection` / `get_token_from_ws` / token close code constants / `MAX_BROADCAST_RETRIES` / `BROADCAST_RETRY_DELAY` / `PING_INTERVAL` / `PONG_TIMEOUT`
**deprecate**：`ChannelHub` class — 加 `DeprecationWarning`，但不立刻刪除（避免 phase-1/2a rebase 黏到 lifecycle 問題）。下下版移除。

### 4.5 `config/cache.py` 擴充

```python
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
    pubsub_timeout_seconds: float = 5.0           # NEW
    publish_payload_max_bytes: int = 8192          # NEW

    @model_validator(mode="after")
    def _validate_redis_url(self) -> "CacheSettings":
        if self.backend == "redis" and not self.redis_url:
            raise ValueError("CACHE_REDIS_URL is required when CACHE_BACKEND=redis")
        return self
```

新 env 變數（部署 runbook）：

```
# .env.example
CACHE_BACKEND=memory                      # prod 設 redis
CACHE_REDIS_URL=                          # redis://default:xxx@redis.zeabur.internal:6379/0
CACHE_KEY_PREFIX=ivy
CACHE_PUBSUB_TIMEOUT_SECONDS=5.0
CACHE_PUBLISH_PAYLOAD_MAX_BYTES=8192
```

## 5. 失效模式與 observability

### 5.1 Failure modes

| 失敗 | 行為 | 觀察 |
|------|------|------|
| `publish()` 時 Redis 斷線 | 本地 dispatch 成功；Redis call 失敗 → log WARN + Sentry 限頻 capture | 同 instance 收得到；跨 instance 收不到 |
| `pump_task` 拋例外 | exponential backoff（1s→30s cap）後 reconnect | log WARN + Sentry 限頻 |
| Redis 啟動時 unreachable | `start()` 拋例外 → uvicorn lifespan 啟動失敗 → Zeabur 重啟（**fail-loud** 設計：prod 沒 Redis 就不該啟動，避免 silent degraded） |
| ws send 失敗 | sweep dead WS（與現況一致） | 既有 WARN log |
| Payload 超過 max bytes | publish 直接 raise `ValueError` | caller 看到 exception；prod 由 Sentry 抓 |

**設計選擇說明**：
- `start()` 階段 **fail-loud**（Redis 連不到直接擋啟動）— 避免 prod 設了 `CACHE_BACKEND=redis` 卻 silent 跑 memory 模式造成跨 instance 看似正常但廣播都掉
- `publish()` / `pump` 階段 **fail-open**（限頻 log 後本地繼續跑）— prod 已啟動後 Redis 短暫掉線不該擋住整個服務
- 一個 fail-loud 一個 fail-open 是刻意的：「啟動時」與「運轉時」對失敗的容忍度不同

### 5.2 Sentry / observability

- Sentry tag: `broadcast.backend=redis|memory`（每筆 capture 自動帶）
- Sentry 限頻：60 秒內最多 1 次 `capture_exception`（避免 Redis 長期掛掉時噴爆 quota）
- log fields：`channel`, `failure_kind=publish|pump|reconnect`
- **不**加 `/health` Redis 探活 — broadcast degraded 不該擋 healthcheck，由 Sentry alert + Zeabur Redis dashboard 監控

### 5.3 不做的事

- ❌ Streams + consumer group → 不需要 at-least-once
- ❌ persistence layer for missed messages → 來源都有 DB 持久化（dismissal_call / contact_book_entry / notification_logs）
- ❌ client-side broadcast ack → best-effort 不需要

## 6. Rollout 步驟

單一 PR，rollout 走 env flag：

1. **Merge PR**：程式碼上 prod，`CACHE_BACKEND=memory`（預設）→ `LocalBackend` 行為與現況 byte-identical。已落地的 4 個 hub 改用 backend，但 backend 仍 in-process。**這時 prod 應該完全感覺不到任何變化**。
2. **Zeabur 部署 Redis template** → 拿到 internal URL（`redis://default:xxx@redis.zeabur.internal:6379/0`）
3. **Staging 環境**設 `CACHE_BACKEND=redis` + `CACHE_REDIS_URL=...` → manual smoke：
   - 開兩個 browser tab 連到不同 staging instance（透過 Zeabur scale 到 2 instance）
   - 一邊送出 dismissal call → 另一邊應該收到 admin 廣播
   - 一邊家長 ack 聯絡簿 → 另一邊教師端應該收到 ack 更新
4. **Prod 切 `CACHE_BACKEND=redis`** + 設 `CACHE_REDIS_URL` → Zeabur 重啟一次即生效
5. **觀察一週**：Sentry `broadcast.backend=redis` tag 看 publish/pump 失敗率、Zeabur Redis memory metric 看 pubsub buffer
6. **一週穩定** → phase-1/2a rebase 之上 merge，享受 fanout 不需再改一行

**回退**：env 切回 `memory`，restart。無 schema 變動、無資料遷移、無 client 端配合。

## 7. 測試策略

### 7.1 Unit tests（`tests/test_broadcast_local.py` + `tests/test_broadcast_redis.py`）

`LocalBackend`：
- subscribe / unsubscribe 行為
- publish 推給 channel 訂閱者
- publish 不推給其他 channel
- send 失敗 sweep dead WS
- payload size guard raise ValueError

`RedisBackend`（用 `fakeredis-py`）：
- start() 建 pubsub + 啟 pump
- publish 同時走本地 + Redis
- pump 流入訊息走本地 dispatch
- Redis 連不到時 publish 不 raise（fail-open）
- payload size guard raise ValueError
- stop() graceful cancel pump task

### 7.2 Integration tests（`tests/test_broadcast_integration.py`）

- 兩個 `RedisBackend` instance 共用同個 `fakeredis` server，A.publish → B 的 subscriber 收得到（驗證 fanout）
- 兩個 `LocalBackend` 不共享 → 驗證隔離（確認沒有 module-level state 偷渡）

### 7.3 既有 WS test 改造（`tests/test_dismissal_ws_*` / `tests/test_contact_book_ws_*`）

Fixture 從直接操作 `dismissal_manager` / `hub` 改為：

```python
@pytest.fixture
def broadcast():
    from utils.broadcast import get_broadcast, reset_for_tests
    reset_for_tests()
    backend = get_broadcast()
    yield backend
    reset_for_tests()
```

所有既有 WS test **行為斷言不變**，只換取得 backend 的方式。

### 7.4 依賴

```python
# requirements.txt（新增）
redis>=5.0,<6                  # async client (redis.asyncio)

# requirements-dev.txt（新增）
fakeredis>=2.20,<3             # 含 aioredis interface
```

## 8. 風險與緩解

| 風險 | 影響 | 緩解 |
|------|------|------|
| `aioredis.PubSub` echo own publish | 同 instance 收兩次訊息（本地 + pump） | implementation 階段加測試實測；若 echo 加 instance UUID + dedupe set |
| Pump task memory leak（subscriber 不清） | 長期 instance 不健康 | unsubscribe 在 `_run_connection` cleanup 中保證呼叫；加 `test_broadcast_pump_lifecycle.py` 驗證 stop/restart 不洩漏 |
| Payload > 8KB 截斷 | 訊息變垃圾 JSON | publish 前 size check raise（**fail-loud** 比 silent truncate 好） |
| Redis pubsub connection 跟 cache connection 競爭 | cache 用同 pool 時被 pubsub blocking call block | `pubsub()` 內部自動 lease 獨立 connection（redis-py 設計），與 publish/cache 不衝突 |
| 多 instance 同收一筆 event 後本地都 push 一次 | OK 預期行為 | 每個 instance push 給「自己這邊連的 subscriber」，總體訊息不重複；spec 記載 |
| Redis password rotation / URL 變更 | restart 期間 memory mode 接住事件 | env 改了 restart 自然重連；intra-restart 期間 fail-open |
| phase-1/2a rebase 衝突 | merge 痛 | broadcast_classroom / broadcast_parent helper 簽章保留，phase-1/2a 內部 caller 不用改 |

## 9. 驗收條件

PR merge 前 checklist：
- [ ] `LocalBackend` 行為與既有 `DismissalConnectionManager.broadcast` + `ChannelHub.broadcast` byte-identical（測試証明）
- [ ] `RedisBackend` 用 fakeredis 跨「兩 backend instance」驗證 fanout
- [ ] 既有 dismissal / contact_book WS test 全綠（不改斷言）
- [ ] `CACHE_BACKEND=memory` 預設值下 prod 行為不變
- [ ] `CACHE_BACKEND=redis` 缺 `CACHE_REDIS_URL` 時啟動 fail-loud
- [ ] `main.py` lifespan startup 呼叫 `get_broadcast().start()`、shutdown 呼叫 `stop()`
- [ ] requirements.txt 加 `redis>=5.0`
- [ ] requirements-dev.txt 加 `fakeredis>=2.20`
- [ ] `.env.example` 補 5 個 cache.* 變數
- [ ] spec `docs/superpowers/specs/2026-05-26-ws-broadcast-redis-pubsub-design.md` 入 main

PR merge 後：
- [ ] Zeabur 部署 Redis template、設 prod env vars
- [ ] Staging 兩 instance 跨 instance smoke 驗證
- [ ] Prod 切換、觀察一週 Sentry 無異常
- [ ] phase-1/2a rebase merge

## 10. Follow-ups（不在本 spec 範圍）

- [ ] Redis-backed cache layer 接上（`config/cache.py` PR2 work，session cache / dedupe / OQ cache 可享受同 Redis）
- [ ] Rate limit 升級 Redis backend（目前 PostgresLimiter 堪用，性能瓶頸再評估）
- [ ] `utils/ws_hub.ChannelHub` 下下版移除（標 DeprecationWarning 後）
- [ ] WS broadcast metrics dashboard（Zeabur Redis pubsub 流量、publish latency p95）
- [ ] `dismissal_manager` alias 下下版移除
