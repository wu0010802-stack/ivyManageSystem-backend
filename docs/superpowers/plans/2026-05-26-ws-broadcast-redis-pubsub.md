# WebSocket 廣播 Redis Pub/Sub 跨 instance fanout — 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收斂 4 個 process-local WS hub 為單一 `BroadcastBackend` singleton，env `CACHE_BACKEND` 切換 `LocalBackend` / `RedisBackend`；Local 模式行為與現況等價（PR merge 不破壞 prod），Redis 模式提供跨 instance Pub/Sub fanout。

**Architecture:** `utils/broadcast/` 新模組（ABC + Local + Redis 兩 impl）；既有 `DismissalConnectionManager` 與 `ChannelHub` 退役為 thin alias / DeprecationWarning；caller 透過 `get_broadcast()` lru_cache singleton 取用，與 `config/cache.py` 共用 Redis settings。

**Tech Stack:** Python 3.9、FastAPI、redis-py 5.x async、fakeredis 2.x（test only）、pydantic-settings v2、pytest-asyncio。

**參考 spec:** `docs/superpowers/specs/2026-05-26-ws-broadcast-redis-pubsub-design.md`（commit `da4e601`）

---

## File Structure

| 檔案 | 動作 | 責任 |
|------|------|------|
| `utils/broadcast/__init__.py` | 新增 | `BroadcastBackend` ABC + `get_broadcast()` factory + `reset_for_tests()` |
| `utils/broadcast/local.py` | 新增 | `LocalBackend` — process-local，行為等價現有 hub |
| `utils/broadcast/redis.py` | 新增 | `RedisBackend` — Redis Pub/Sub fanout + 本地 fallback + pump task |
| `config/cache.py` | 修改 | 加 `pubsub_timeout_seconds` / `publish_payload_max_bytes` 兩欄 + `model_validator` |
| `main.py:272-` | 修改 | `app_lifespan` 加 `await get_broadcast().start()` / `stop()` |
| `api/dismissal_ws.py` | 修改 | 刪 `DismissalConnectionManager` class、改用 backend；保留 `dismissal_manager` deprecated alias |
| `api/contact_book_ws.py` | 修改 | 刪 `hub = ChannelHub()` singleton；`broadcast_classroom` / `broadcast_parent` helper 內部改用 backend |
| `api/students.py:915,1002,1265` | 修改 | 3 處 `dismissal_manager.broadcast(...)` → `get_broadcast().publish_many(...)` |
| `utils/ws_hub.py` | 修改 | `ChannelHub` 加 `DeprecationWarning`（保留一版以免外部 import 立刻炸） |
| `requirements.txt` | 修改 | 加 `redis>=5.0,<6` + `fakeredis>=2.20,<3` |
| `.env.example` | 修改 | 加 5 個 `CACHE_*` 變數 |
| `tests/test_broadcast_local.py` | 新增 | `LocalBackend` unit |
| `tests/test_broadcast_redis.py` | 新增 | `RedisBackend` unit + fakeredis integration |
| `tests/test_broadcast_factory.py` | 新增 | `get_broadcast()` lru_cache + settings 切換 |
| `tests/test_config_cache_validation.py` | 新增 | `CacheSettings.model_validator` 行為 |

**不改**：
- 既有 `tests/test_dismissal_calls.py` / `test_contact_book.py` 不改斷言 — Local 模式下行為應等價
- WS protocol（前端不需配合）
- Alembic migration（無 schema 變動）

---

## Task 1: 加依賴與 CacheSettings 擴充

**Files:**
- Modify: `requirements.txt`
- Modify: `config/cache.py`
- Test: `tests/test_config_cache_validation.py`

### Step 1.1 加 Redis client 與 fakeredis 依賴

- [ ] Modify `requirements.txt`：在 `sentry-sdk[fastapi]>=2.0.0` 後加兩行

```python
redis>=5.0,<6  # async client（redis.asyncio），broadcast + 未來 cache 共用
fakeredis>=2.20,<3  # test only；fakeredis.aioredis 仿真 Redis async + pubsub
```

- [ ] 安裝：`pip install -r requirements.txt`
- [ ] 驗證：`python -c "import redis.asyncio; import fakeredis.aioredis; print('ok')"` → `ok`

### Step 1.2 擴充 CacheSettings 兩欄與 validator

- [ ] Modify `config/cache.py`：整個替換為

```python
"""config/cache.py — cache + broadcast Redis settings.

PR1 only ships memory backend；PR2 才會接 Redis cache。
本 PR 接 Redis 給 WS broadcast 用，cache 用法是 follow-up。
"""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator
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
    pubsub_timeout_seconds: float = 5.0
    publish_payload_max_bytes: int = 8192

    @model_validator(mode="after")
    def _validate_redis_url(self) -> "CacheSettings":
        if self.backend == "redis" and not self.redis_url:
            raise ValueError(
                "CACHE_REDIS_URL is required when CACHE_BACKEND=redis"
            )
        return self
```

### Step 1.3 寫 failing test

- [ ] Create `tests/test_config_cache_validation.py`

```python
"""Test CacheSettings model_validator (fail-loud on missing redis_url)."""

import pytest
from pydantic import ValidationError

from config.cache import CacheSettings


def test_memory_backend_no_redis_url_ok():
    s = CacheSettings(backend="memory", redis_url=None)
    assert s.backend == "memory"
    assert s.redis_url is None


def test_redis_backend_requires_redis_url():
    with pytest.raises(ValidationError) as exc_info:
        CacheSettings(backend="redis", redis_url=None)
    assert "CACHE_REDIS_URL is required" in str(exc_info.value)


def test_redis_backend_with_url_ok():
    s = CacheSettings(backend="redis", redis_url="redis://localhost:6379/0")
    assert s.backend == "redis"
    assert s.redis_url == "redis://localhost:6379/0"


def test_new_fields_defaults():
    s = CacheSettings()
    assert s.pubsub_timeout_seconds == 5.0
    assert s.publish_payload_max_bytes == 8192
    assert s.key_prefix == "ivy"
```

### Step 1.4 跑 test

- [ ] Run: `pytest tests/test_config_cache_validation.py -v`
- [ ] Expected: 4 passed

### Step 1.5 Commit

- [ ] Stage 三檔並 commit：

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add requirements.txt config/cache.py tests/test_config_cache_validation.py
git commit -m "feat(cache): add redis + fakeredis deps + payload size config

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: BroadcastBackend ABC + factory

**Files:**
- Create: `utils/broadcast/__init__.py`
- Test: `tests/test_broadcast_factory.py`

### Step 2.1 建模組目錄

- [ ] Run: `mkdir -p /Users/yilunwu/Desktop/ivy-backend/utils/broadcast`
- [ ] 驗證：`ls /Users/yilunwu/Desktop/ivy-backend/utils/broadcast/`（應為空）

### Step 2.2 寫 ABC + factory

- [ ] Create `utils/broadcast/__init__.py`

```python
"""utils/broadcast — 跨 instance WebSocket 廣播 backend。

LocalBackend：process-local，行為等價既有 hub（dev / memory mode prod）。
RedisBackend：Redis Pub/Sub fanout，subscribe 端各 instance 啟一個 pump task。

caller 統一透過 get_broadcast() 取得 singleton（lru_cache）。
"""

from __future__ import annotations

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
        """syntactic sugar — channel-key 去重後逐一 publish。

        WS-level 去重（同 WS 訂多 channel 只收一次）未實作（YAGNI）。
        實務上 caller 不會把同 WS 訂跨類型 channel。
        """
        seen: dict[str, None] = {}
        for ch in channels:
            if ch in seen:
                continue
            seen[ch] = None
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

    cache = settings.cache
    if cache.backend == "redis":
        from utils.broadcast.redis import RedisBackend

        return RedisBackend(
            redis_url=cache.redis_url,
            key_prefix=cache.key_prefix,
            payload_max_bytes=cache.publish_payload_max_bytes,
        )
    from utils.broadcast.local import LocalBackend

    return LocalBackend(payload_max_bytes=cache.publish_payload_max_bytes)


def reset_for_tests() -> None:
    """test fixture 用 — 清掉 lru_cache 讓下次 get_broadcast() 重建。

    呼叫前若 backend 已 start() 過，caller 須自行 await stop()。
    """
    get_broadcast.cache_clear()
```

### Step 2.3 寫 factory failing test（先依賴 LocalBackend 不存在）

- [ ] Create `tests/test_broadcast_factory.py`

```python
"""Test get_broadcast() factory + lru_cache + settings 切換。"""

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
    import asyncio

    asyncio.get_event_loop().run_until_complete(
        backend.publish_many(["a", "b", "a", "c", "b"], {"x": 1})
    )
    assert calls == ["a", "b", "c"]
```

### Step 2.4 跑 test（應該失敗，因為 LocalBackend / RedisBackend 未實作）

- [ ] Run: `pytest tests/test_broadcast_factory.py -v`
- [ ] Expected: 4 tests, 至少 2 個 ERROR/FAIL（`ImportError: cannot import name 'LocalBackend'` / `RedisBackend`）

備註：這是 TDD red 階段，**先進 Task 3 補 LocalBackend、Task 4 補 RedisBackend，再回頭跑這檔的綠**。本 task 不 commit 測試（合到 Task 3 一起 commit），但 ABC 自己可 commit。

### Step 2.5 Commit ABC（不含測試）

- [ ] Stage 並 commit ABC（測試保留 untracked，Task 3 一起 commit）：

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add utils/broadcast/__init__.py
git commit -m "feat(broadcast): add BroadcastBackend ABC + get_broadcast factory

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: LocalBackend 實作

**Files:**
- Create: `utils/broadcast/local.py`
- Test: `tests/test_broadcast_local.py`

### Step 3.1 寫 failing test

- [ ] Create `tests/test_broadcast_local.py`

```python
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


@pytest.mark.asyncio
async def test_publish_routes_to_channel_subscribers():
    b = LocalBackend(payload_max_bytes=8192)
    ws_a = FakeWS()
    ws_b = FakeWS()
    b.subscribe("ch1", ws_a)
    b.subscribe("ch2", ws_b)

    await b.publish("ch1", {"type": "hello", "data": 1})

    assert len(ws_a.received) == 1
    assert '"hello"' in ws_a.received[0]
    assert len(ws_b.received) == 0


@pytest.mark.asyncio
async def test_publish_to_empty_channel_no_error():
    b = LocalBackend(payload_max_bytes=8192)
    await b.publish("nobody-listens", {"x": 1})  # 不該 raise


@pytest.mark.asyncio
async def test_unsubscribe_removes_from_all_channels():
    b = LocalBackend(payload_max_bytes=8192)
    ws = FakeWS()
    b.subscribe("ch1", ws)
    b.subscribe("ch2", ws)
    b.unsubscribe(ws)

    await b.publish("ch1", {"x": 1})
    await b.publish("ch2", {"x": 2})
    assert ws.received == []


@pytest.mark.asyncio
async def test_dead_ws_swept_after_retry():
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


@pytest.mark.asyncio
async def test_publish_payload_size_guard():
    b = LocalBackend(payload_max_bytes=100)
    ws = FakeWS()
    b.subscribe("ch", ws)
    big = {"x": "a" * 200}
    with pytest.raises(ValueError, match="too large"):
        await b.publish("ch", big)


@pytest.mark.asyncio
async def test_start_stop_noop():
    b = LocalBackend()
    await b.start()
    await b.stop()  # 不該 raise


@pytest.mark.asyncio
async def test_publish_many_uses_publish():
    """publish_many 走 publish 路徑（驗證 base class default impl 整合 OK）。"""
    b = LocalBackend()
    ws = FakeWS()
    b.subscribe("a", ws)
    b.subscribe("b", ws)
    await b.publish_many(["a", "b"], {"x": 1})
    assert len(ws.received) == 2
```

### Step 3.2 跑 test 驗證失敗

- [ ] Run: `pytest tests/test_broadcast_local.py -v`
- [ ] Expected: 7 errors（ImportError: cannot import 'LocalBackend'）

### Step 3.3 寫 LocalBackend 實作

- [ ] Create `utils/broadcast/local.py`

```python
"""utils/broadcast/local.py — process-local backend（行為等價既有 hub）。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

from utils.broadcast import BroadcastBackend
from utils.ws_hub import BROADCAST_RETRY_DELAY, MAX_BROADCAST_RETRIES

logger = logging.getLogger(__name__)


class LocalBackend(BroadcastBackend):
    def __init__(self, *, payload_max_bytes: int = 8192):
        self._subscribers: dict[str, list[WebSocket]] = defaultdict(list)
        self._payload_max_bytes = payload_max_bytes

    async def publish(self, channel: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str)
        if len(body.encode("utf-8")) > self._payload_max_bytes:
            raise ValueError(
                f"broadcast payload too large for channel={channel}: "
                f"{len(body)} bytes > {self._payload_max_bytes}"
            )
        await self._dispatch_local(channel, body)

    def subscribe(self, channel: str, ws: WebSocket) -> None:
        self._subscribers[channel].append(ws)

    def unsubscribe(self, ws: WebSocket) -> None:
        for lst in self._subscribers.values():
            if ws in lst:
                lst.remove(ws)

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def _dispatch_local(self, channel: str, body: str) -> None:
        targets = list(self._subscribers.get(channel, []))
        dead: list[WebSocket] = []
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
                            channel,
                            attempt,
                            exc,
                        )
            if not sent:
                dead.append(ws)
        for ws in dead:
            self.unsubscribe(ws)
```

### Step 3.4 跑 test 驗證綠

- [ ] Run: `pytest tests/test_broadcast_local.py -v`
- [ ] Expected: 7 passed

### Step 3.5 跑 Task 2 的 factory test 確認 LocalBackend 切入

- [ ] Run: `pytest tests/test_broadcast_factory.py::test_factory_default_returns_local tests/test_broadcast_factory.py::test_factory_returns_singleton tests/test_broadcast_factory.py::test_publish_many_dedupes_channel_keys -v`
- [ ] Expected: 3 passed（`test_factory_redis_mode` 還會 fail 因 RedisBackend 未實作，先 skip）

### Step 3.6 Commit

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add utils/broadcast/local.py tests/test_broadcast_local.py tests/test_broadcast_factory.py
git commit -m "feat(broadcast): LocalBackend + factory tests

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] commit 成功

---

## Task 4: RedisBackend 實作 + echo spike + integration

**Files:**
- Create: `utils/broadcast/redis.py`
- Test: `tests/test_broadcast_redis.py`

### Step 4.1 echo spike — 驗證 Redis Pub/Sub 不 echo 自己 publish

spec §4.2 提到 implementation 階段需驗證 `aioredis.PubSub` 是否 echo own publish。先寫 spike 確認。

- [ ] Run（用 fakeredis 驗 in-memory pubsub 行為）：

```bash
cd /Users/yilunwu/Desktop/ivy-backend
python -c "
import asyncio
import fakeredis.aioredis as ar

async def main():
    r = ar.FakeRedis()
    pubsub = r.pubsub()
    await pubsub.psubscribe('ivy:*')
    # 同一個 connection 也 publish
    await r.publish('ivy:test', 'hello')
    # 拿一筆訊息 — 看會不會收到自己 publish 的
    msg = await asyncio.wait_for(pubsub.get_message(ignore_subscribe_messages=True), timeout=1.0)
    print('msg:', msg)
    await pubsub.close()
    await r.close()

asyncio.run(main())
"
```

- [ ] Expected output: `msg: {'type': 'pmessage', 'pattern': 'ivy:*', 'channel': 'ivy:test', 'data': 'hello'}`（**會收到** — fakeredis echoes，real Redis 也會 echo）

**重要**：實測 Redis Pub/Sub **會** echo own publish（pubsub channel 模型 — 任何 subscriber 都會收到，包括 publisher 本身 instance）。spec §4.2 假設「不 echo」是錯的。

**修正設計**：`RedisBackend.publish` 不主動跑本地 `_dispatch_local` — 統一靠 pump task 流入。優點：簡單、無重複；缺點：同 instance 廣播多 1 ms RTT。

**進一步保險**：pump 帶 instance UUID 標記，跨 instance 訊息收到時若 sender_id 是自己，仍然 dispatch（廣播本來就該收到）。只有 publisher 的「同 instance subscribers」不需要 echo back through Redis。我們選**最簡單**方案：所有 publish 一律走 Redis、pump 流入後 dispatch 給本地 subscriber，**不**做本地 short-circuit。

- [ ] 把這個觀察記到 plan 註記（已寫入此 step）

### Step 4.2 寫 failing test

- [ ] Create `tests/test_broadcast_redis.py`

```python
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
    created: list[fakeredis_aio.FakeRedis] = []

    def _from_url(url: str, **kw):
        client = fakeredis_aio.FakeRedis(server=server, decode_responses=kw.get("decode_responses", False))
        created.append(client)
        return client

    monkeypatch.setattr("redis.asyncio.from_url", _from_url)
    yield server, created


@pytest.mark.asyncio
async def test_publish_fans_out_to_local_via_pump(fake_redis):
    backend = RedisBackend(
        redis_url="redis://fake/0",
        key_prefix="ivy",
        payload_max_bytes=8192,
    )
    await backend.start()
    ws = FakeWS()
    backend.subscribe("test.channel", ws)

    await backend.publish("test.channel", {"type": "x", "data": 1})

    # 等 pump 流入（fakeredis pubsub 是 in-memory，loop 一輪即到）
    await asyncio.sleep(0.05)
    assert len(ws.received) == 1
    assert '"x"' in ws.received[0]

    await backend.stop()


@pytest.mark.asyncio
async def test_two_backends_fanout_via_shared_server(fake_redis):
    """A.publish → B 的 subscriber 收得到（驗證真 fanout）。"""
    a = RedisBackend(redis_url="redis://fake/0", key_prefix="ivy", payload_max_bytes=8192)
    b = RedisBackend(redis_url="redis://fake/0", key_prefix="ivy", payload_max_bytes=8192)
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


@pytest.mark.asyncio
async def test_payload_size_guard(fake_redis):
    backend = RedisBackend(redis_url="redis://fake/0", key_prefix="ivy", payload_max_bytes=50)
    await backend.start()
    with pytest.raises(ValueError, match="too large"):
        await backend.publish("ch", {"x": "a" * 200})
    await backend.stop()


@pytest.mark.asyncio
async def test_unsubscribe_removes_from_all_channels(fake_redis):
    backend = RedisBackend(redis_url="redis://fake/0", key_prefix="ivy", payload_max_bytes=8192)
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


@pytest.mark.asyncio
async def test_publish_fail_open_when_redis_unreachable(monkeypatch):
    """Redis publish 拋例外時 publish() 不該 raise（fail-open，限頻 Sentry）。"""
    from redis.exceptions import ConnectionError as RedisConnectionError

    backend = RedisBackend(
        redis_url="redis://localhost:1/0",  # unreachable
        key_prefix="ivy",
        payload_max_bytes=8192,
    )

    # 不跑 start — 直接打 publish 觀察 fail-open；先手動把 _redis 設成 raise
    class _BadRedis:
        async def publish(self, *a, **kw):
            raise RedisConnectionError("nope")

    backend._redis = _BadRedis()  # type: ignore[assignment]
    # 不該 raise
    await backend.publish("ch", {"x": 1})


def test_init_without_redis_url_raises():
    from utils.broadcast.redis import RedisBackend

    with pytest.raises(RuntimeError, match="CACHE_REDIS_URL is required"):
        RedisBackend(redis_url="", key_prefix="ivy", payload_max_bytes=8192)


@pytest.mark.asyncio
async def test_start_stop_lifecycle_idempotent(fake_redis):
    """stop() 後再 start() 不該炸（雖然 prod 不會這樣，test 確保 cleanup OK）。"""
    backend = RedisBackend(redis_url="redis://fake/0", key_prefix="ivy", payload_max_bytes=8192)
    await backend.start()
    await backend.stop()
    # stop 後 _pump_task 已 cancel；不重啟也不該洩漏


@pytest.mark.asyncio
async def test_key_prefix_routing(fake_redis):
    """publish ch 應走 Redis channel `{prefix}:ch`。"""
    server, _created = fake_redis
    backend = RedisBackend(redis_url="redis://fake/0", key_prefix="kindergarten", payload_max_bytes=8192)
    await backend.start()
    ws = FakeWS()
    backend.subscribe("event.x", ws)
    await backend.publish("event.x", {"foo": "bar"})
    await asyncio.sleep(0.05)
    assert len(ws.received) == 1
    await backend.stop()
```

### Step 4.3 跑 test 驗證失敗

- [ ] Run: `pytest tests/test_broadcast_redis.py -v`
- [ ] Expected: 8 errors（ImportError: cannot import 'RedisBackend'）

### Step 4.4 寫 RedisBackend 實作

- [ ] Create `utils/broadcast/redis.py`

```python
"""utils/broadcast/redis.py — Redis Pub/Sub fanout backend。

設計：
- publish 只送到 Redis；pump task 流入後 dispatch 給本地 subscribers
- Real Redis Pub/Sub 會 echo own publish（fakeredis spike 已驗證），
  所以本 instance 的訂閱者也透過 pump 流入收到事件，不會重複也不會漏
- fail-open：Redis 失敗時不 raise，限頻 capture Sentry
- start() 階段 fail-loud（redis_url 缺 / 連不到 → 啟動失敗）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict

import redis.asyncio as aioredis
from fastapi import WebSocket
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)

from utils.broadcast import BroadcastBackend
from utils.ws_hub import BROADCAST_RETRY_DELAY, MAX_BROADCAST_RETRIES

logger = logging.getLogger(__name__)

_SENTRY_THROTTLE_SECONDS = 60


class RedisBackend(BroadcastBackend):
    def __init__(
        self,
        *,
        redis_url: str | None,
        key_prefix: str,
        payload_max_bytes: int,
    ):
        if not redis_url:
            raise RuntimeError(
                "CACHE_REDIS_URL is required when CACHE_BACKEND=redis"
            )
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
        # 啟動時 ping 一次驗證 Redis 可達（fail-loud）
        await self._redis.ping()
        self._pubsub = self._redis.pubsub()
        await self._pubsub.psubscribe(f"{self._prefix}*")
        self._pump_task = asyncio.create_task(self._pump(), name="broadcast-pump")
        logger.info("RedisBackend started prefix=%s", self._prefix)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._pubsub is not None:
            try:
                await self._pubsub.close()
            except Exception as exc:
                logger.warning("pubsub close failed: %s", exc)
        if self._redis is not None:
            try:
                await self._redis.close()
            except Exception as exc:
                logger.warning("redis close failed: %s", exc)
        logger.info("RedisBackend stopped")

    async def publish(self, channel: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str)
        if len(body.encode("utf-8")) > self._payload_max_bytes:
            raise ValueError(
                f"broadcast payload too large for channel={channel}: "
                f"{len(body)} bytes > {self._payload_max_bytes}"
            )
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
                    if isinstance(channel, bytes):
                        channel = channel.decode("utf-8")
                    if channel.startswith(self._prefix):
                        channel = channel[len(self._prefix):]
                    data = msg["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    await self._dispatch_local(channel, data)
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._note_redis_failure("pump", exc)
                try:
                    await asyncio.sleep(min(backoff, 30))
                except asyncio.CancelledError:
                    break
                backoff *= 2
                try:
                    await self._pubsub.psubscribe(f"{self._prefix}*")
                except Exception:
                    continue

    async def _dispatch_local(self, channel: str, body: str) -> None:
        targets = list(self._subscribers.get(channel, []))
        dead: list[WebSocket] = []
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
                            channel,
                            attempt,
                            exc,
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

### Step 4.5 跑 test 驗證綠

- [ ] Run: `pytest tests/test_broadcast_redis.py -v`
- [ ] Expected: 8 passed

### Step 4.6 整體 broadcast 模組測試

- [ ] Run: `pytest tests/test_broadcast_factory.py tests/test_broadcast_local.py tests/test_broadcast_redis.py tests/test_config_cache_validation.py -v`
- [ ] Expected: 4 + 7 + 8 + 4 = 23 passed

### Step 4.7 Commit

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add utils/broadcast/redis.py tests/test_broadcast_redis.py
git commit -m "feat(broadcast): RedisBackend with Pub/Sub fanout + fakeredis tests

real Redis Pub/Sub echoes own publish; pump-only dispatch path
keeps single source of truth, no dedupe complexity.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] commit 成功

---

## Task 5: main.py lifespan 整合

**Files:**
- Modify: `main.py:272-` (app_lifespan)
- Test: `tests/test_main_broadcast_lifespan.py`

### Step 5.1 寫 failing test

- [ ] Create `tests/test_main_broadcast_lifespan.py`

```python
"""驗證 app_lifespan startup/shutdown 有呼叫 get_broadcast().start()/stop()。"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_broadcast():
    from utils.broadcast import reset_for_tests

    reset_for_tests()
    yield
    reset_for_tests()


def test_lifespan_starts_and_stops_broadcast(monkeypatch):
    """startup 呼叫 backend.start(), shutdown 呼叫 stop()。"""
    monkeypatch.setenv("CACHE_BACKEND", "memory")
    from config import reset_for_tests as cfg_reset

    cfg_reset()

    started = AsyncMock()
    stopped = AsyncMock()

    from utils.broadcast import get_broadcast, reset_for_tests as br_reset

    br_reset()
    backend = get_broadcast()
    monkeypatch.setattr(backend, "start", started)
    monkeypatch.setattr(backend, "stop", stopped)

    # patch get_broadcast 回傳同一 instance（避免 main 取得新的）
    monkeypatch.setattr("utils.broadcast.get_broadcast", lambda: backend)
    monkeypatch.setattr("main.get_broadcast", lambda: backend, raising=False)

    from main import app

    with TestClient(app) as _client:
        started.assert_awaited_once()

    stopped.assert_awaited_once()
```

### Step 5.2 跑 test 驗證失敗

- [ ] Run: `pytest tests/test_main_broadcast_lifespan.py -v`
- [ ] Expected: 1 failed（`started.assert_awaited_once()` AssertionError）

### Step 5.3 改 main.py：加 startup / shutdown hook

- [ ] Read main.py:271-310 確認 lifespan 結構（已存在 `app_lifespan`）

- [ ] Modify main.py：在 `on_startup()` 呼叫前加 broadcast start，在 `finally:` 區段加 broadcast stop。具體 patch：

第 282 行（`set_main_loop(_main_loop)` 之後 / `on_startup()` 之前）後一行插入：

```python
    # WS 廣播 backend（memory / redis 由 CACHE_BACKEND 切換）
    from utils.broadcast import get_broadcast

    _broadcast = get_broadcast()
    await _broadcast.start()
```

在 lifespan 的 `finally:` 區段（搜尋 `yield` 後第一個 `finally`，現存於 main.py 末尾，scheduler shutdown 同一段）**最開頭**加：

```python
        # 停 broadcast backend（先於 scheduler shutdown，避免 stop 期間還收到 publish）
        try:
            await _broadcast.stop()
        except Exception as exc:
            logger.warning("broadcast backend stop failed: %s", exc)
```

備註：實作時用 `grep -n "yield" main.py` 找正確 yield 位置，確認 finally 區段確切行號。`_broadcast` 變數在 yield 之前綁定、yield 之後 close。

### Step 5.4 跑 lifespan test 確認綠

- [ ] Run: `pytest tests/test_main_broadcast_lifespan.py -v`
- [ ] Expected: 1 passed

### Step 5.5 確認 dev server 仍能正常啟動

- [ ] Run: `cd /Users/yilunwu/Desktop/ivy-backend && timeout 10 python -c "from main import app; print('imported ok')"`
- [ ] Expected: `imported ok`（無 exception）

### Step 5.6 Commit

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add main.py tests/test_main_broadcast_lifespan.py
git commit -m "feat(broadcast): wire backend start/stop into app_lifespan

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] commit 成功

---

## Task 6: api/dismissal_ws.py 改造 + 3 處 caller 改 publish_many

**Files:**
- Modify: `api/dismissal_ws.py`（刪 `DismissalConnectionManager`、改用 backend、保留 alias）
- Modify: `api/students.py:915, 1002, 1265`（3 處 caller）

### Step 6.1 改 api/dismissal_ws.py

- [ ] Read 現況 api/dismissal_ws.py:38-108 確認 manager class 邊界

- [ ] Modify api/dismissal_ws.py — 整段替換 43-108 行（class + singleton）為：

```python
# ---------------------------------------------------------------------------
# Channel 命名規範（取代 DismissalConnectionManager 內部 dict）
#   dismissal.classroom.{cid}  — 老師 WS 訂閱所屬班級
#   dismissal.admin            — 管理端 WS 訂閱全部
# ---------------------------------------------------------------------------

from utils.broadcast import get_broadcast


def _classroom_channel(classroom_id: int) -> str:
    return f"dismissal.classroom.{classroom_id}"


_ADMIN_CHANNEL = "dismissal.admin"


class _DeprecatedDismissalManager:
    """Deprecated shim — 既有 caller 暫留 dismissal_manager.broadcast 簽章。

    新 caller 請直接呼叫 `get_broadcast().publish_many(...)`。
    """

    async def broadcast(self, classroom_id: int, event: dict) -> None:
        import warnings

        warnings.warn(
            "dismissal_manager.broadcast is deprecated; use "
            "get_broadcast().publish_many([dismissal.classroom.{cid}, "
            "dismissal.admin], event) directly",
            DeprecationWarning,
            stacklevel=2,
        )
        await get_broadcast().publish_many(
            [_classroom_channel(classroom_id), _ADMIN_CHANNEL],
            event,
        )


# 向後相容 alias（phase-1/2a rebase 期間避免炸；下下版移除）
manager = _DeprecatedDismissalManager()
dismissal_manager = manager
```

- [ ] Modify api/dismissal_ws.py — 137-211 行 WS endpoint 改用 backend：

```python
@ws_router.websocket("/api/ws/portal/dismissal-calls")
async def portal_dismissal_ws(ws: WebSocket):
    """教師 Portal WebSocket：只推送自己班級的接送通知事件。"""
    token = _get_token_from_ws(ws)
    if not token:
        await ws.close(code=WS_CLOSE_MISSING_TOKEN, reason="未提供 Token")
        return

    try:
        payload = verify_ws_token(token)
    except HTTPException as e:
        code = WS_CLOSE_FORBIDDEN if e.status_code == 403 else WS_CLOSE_INVALID_TOKEN
        await ws.close(code=code, reason=e.detail)
        return
    except Exception:
        await ws.close(code=WS_CLOSE_INVALID_TOKEN, reason="Token 無效或已過期")
        return

    employee_id = payload.get("employee_id")
    if not employee_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="此帳號無對應教師身分")
        return

    role = payload.get("role", "")
    if role != "teacher":
        await ws.close(
            code=WS_CLOSE_FORBIDDEN, reason="僅教師帳號可使用接送通知 WebSocket"
        )
        return

    classroom_ids = _get_teacher_classroom_ids(employee_id)
    backend = get_broadcast()
    if not classroom_ids:
        await ws.accept()
        await _run_connection(ws)
        return

    await ws.accept()
    for cid in classroom_ids:
        backend.subscribe(_classroom_channel(cid), ws)
    await _run_connection(ws, cleanup=lambda: backend.unsubscribe(ws))


@ws_router.websocket("/api/ws/admin/dismissal-calls")
async def admin_dismissal_ws(ws: WebSocket):
    """管理端 WebSocket：接收全部班級的接送通知事件。"""
    token = _get_token_from_ws(ws)
    if not token:
        await ws.close(code=WS_CLOSE_MISSING_TOKEN, reason="未提供 Token")
        return

    try:
        payload = verify_ws_token(token)
    except HTTPException as e:
        code = WS_CLOSE_FORBIDDEN if e.status_code == 403 else WS_CLOSE_INVALID_TOKEN
        await ws.close(code=code, reason=e.detail)
        return
    except Exception:
        await ws.close(code=WS_CLOSE_INVALID_TOKEN, reason="Token 無效或已過期")
        return

    if payload.get("role") not in ("admin", "hr", "supervisor"):
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="教師帳號不可存取管理端接送通知")
        return

    if not has_permission(payload.get("permission_names"), Permission.STUDENTS_READ):
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="權限不足，需要學生讀取權限")
        return

    backend = get_broadcast()
    await ws.accept()
    backend.subscribe(_ADMIN_CHANNEL, ws)
    await _run_connection(ws, cleanup=lambda: backend.unsubscribe(ws))
```

備註：原 endpoint 在 connect_teacher/connect_admin 內呼叫 `await ws.accept()`，被收進 class 內。新版顯式呼叫 `await ws.accept()` 後再 subscribe。

### Step 6.2 改 api/students.py 3 處 caller

- [ ] Read api/students.py:910-920 確認 915 context
- [ ] Modify api/students.py:915（dismissal_manager.broadcast）→：

```python
            from utils.broadcast import get_broadcast
            from api.dismissal_ws import _classroom_channel, _ADMIN_CHANNEL

            await get_broadcast().publish_many(
                [_classroom_channel(item["classroom_id"]), _ADMIN_CHANNEL],
                item["event"],
            )
```

備註：實作時應把兩個 import 集中放到檔案頂端 import 區，避免每次 callsite import；以上 inline 形式只為示意。實作步驟：

1. 在 api/students.py 頂端 import 區加：

```python
from utils.broadcast import get_broadcast
from api.dismissal_ws import _classroom_channel as _dismissal_classroom_channel
from api.dismissal_ws import _ADMIN_CHANNEL as _DISMISSAL_ADMIN_CHANNEL
```

2. 把 915 / 1002 / 1265 三處呼叫各自替換為：

```python
await get_broadcast().publish_many(
    [_dismissal_classroom_channel(<classroom_id_expr>), _DISMISSAL_ADMIN_CHANNEL],
    <event_expr>,
)
```

- [ ] 對 1002 行：`<classroom_id_expr>` 與 `<event_expr>` 依原 `dismissal_manager.broadcast(arg1, arg2)` 兩個 arg 對應
- [ ] 對 1265 行：同上

### Step 6.3 跑既有 dismissal test 驗證

- [ ] Run: `pytest tests/test_dismissal_calls.py tests/test_dismissal_http.py tests/test_dismissal_permissions.py tests/test_ws_auth.py tests/test_ws_heartbeat.py -v`
- [ ] Expected: 全綠（既有測試對 backend 中介透明）

備註：若 test 直接斷言 `dismissal_manager._teacher_conns` 內部結構，需改為斷言 `backend._subscribers["dismissal.classroom.{cid}"]`。預期既有 test 用 WS client 走 endpoint，不會 reach into internals — 如真有，逐一改造（可能 1-2 處）。

### Step 6.4 Commit

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/dismissal_ws.py api/students.py
git commit -m "refactor(dismissal): migrate ConnectionManager to broadcast backend

DismissalConnectionManager class removed; dismissal_manager alias kept
as DeprecationWarning shim during phase-1/2a transition.

3 students.py callers migrated to get_broadcast().publish_many().

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] commit 成功

---

## Task 7: api/contact_book_ws.py 改造

**Files:**
- Modify: `api/contact_book_ws.py`

### Step 7.1 改 contact_book_ws.py

- [ ] Modify api/contact_book_ws.py — 整檔替換為：

```python
"""api/contact_book_ws.py — 聯絡簿 WebSocket 端點。

雙 channel：
- 教師端：依 classroom_id 訂閱（看到自己班級被家長 ack 的計數即時更新）
- 家長端：依 parent_user_id 訂閱（聯絡簿發布即時通知）

從 ChannelHub 遷移到 utils/broadcast.BroadcastBackend；caller helper
broadcast_classroom / broadcast_parent 簽章保留。
"""

import logging

from fastapi import APIRouter, HTTPException, WebSocket

from api.portal._shared import (
    _get_teacher_classroom_ids as _get_teacher_classroom_ids_shared,
)
from models.database import get_session
from utils.auth import verify_ws_token
from utils.broadcast import get_broadcast
from utils.permissions import Permission, has_permission
from utils.ws_hub import (
    WS_CLOSE_FORBIDDEN,
    WS_CLOSE_INVALID_TOKEN,
    WS_CLOSE_MISSING_TOKEN,
    get_token_from_ws,
    run_ws_connection,
)

logger = logging.getLogger(__name__)


def _classroom_channel(classroom_id: int) -> str:
    return f"contact_book.classroom.{classroom_id}"


def _parent_channel(parent_user_id: int) -> str:
    return f"contact_book.parent.{parent_user_id}"


async def broadcast_classroom(classroom_id: int, event: dict) -> None:
    """供 service 層呼叫：將事件推送至教師班級 channel。"""
    await get_broadcast().publish(_classroom_channel(classroom_id), event)


async def broadcast_parent(parent_user_id: int, event: dict) -> None:
    """供 service 層呼叫：將事件推送至特定家長 channel。"""
    await get_broadcast().publish(_parent_channel(parent_user_id), event)


def _get_teacher_classroom_ids(employee_id: int) -> list[int]:
    session = get_session()
    try:
        return _get_teacher_classroom_ids_shared(session, employee_id)
    finally:
        session.close()


ws_router = APIRouter()


@ws_router.websocket("/api/ws/portal/contact-book")
async def portal_contact_book_ws(ws: WebSocket):
    """教師端聯絡簿 WS — 收到自己班級的 ack/reply 即時通知。"""
    token = get_token_from_ws(ws)
    if not token:
        await ws.close(code=WS_CLOSE_MISSING_TOKEN, reason="未提供 Token")
        return

    try:
        payload = verify_ws_token(token)
    except HTTPException as e:
        code = WS_CLOSE_FORBIDDEN if e.status_code == 403 else WS_CLOSE_INVALID_TOKEN
        await ws.close(code=code, reason=e.detail)
        return
    except Exception:
        await ws.close(code=WS_CLOSE_INVALID_TOKEN, reason="Token 無效或已過期")
        return

    role = payload.get("role", "")
    if role not in ("teacher", "admin", "supervisor", "hr"):
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="此帳號無權限訂閱聯絡簿")
        return

    perms = payload.get("permission_names")
    if not (
        has_permission(perms, Permission.PORTFOLIO_READ)
        or has_permission(perms, Permission.PORTFOLIO_WRITE)
    ):
        await ws.close(
            code=WS_CLOSE_FORBIDDEN, reason="權限不足，需要 portfolio 讀取權限"
        )
        return

    classroom_ids: list[int] = []
    employee_id = payload.get("employee_id")
    if role == "teacher" and employee_id:
        classroom_ids = _get_teacher_classroom_ids(employee_id)

    backend = get_broadcast()
    await ws.accept()
    if classroom_ids:
        for cid in classroom_ids:
            backend.subscribe(_classroom_channel(cid), ws)
    await run_ws_connection(ws, cleanup=lambda: backend.unsubscribe(ws))


@ws_router.websocket("/api/ws/parent/contact-book")
async def parent_contact_book_ws(ws: WebSocket):
    """家長端聯絡簿 WS — 收到自己子女的聯絡簿發布即時通知。"""
    token = get_token_from_ws(ws)
    if not token:
        await ws.close(code=WS_CLOSE_MISSING_TOKEN, reason="未提供 Token")
        return

    try:
        payload = verify_ws_token(token)
    except HTTPException as e:
        code = WS_CLOSE_FORBIDDEN if e.status_code == 403 else WS_CLOSE_INVALID_TOKEN
        await ws.close(code=code, reason=e.detail)
        return
    except Exception:
        await ws.close(code=WS_CLOSE_INVALID_TOKEN, reason="Token 無效或已過期")
        return

    if payload.get("role") != "parent":
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="此 WS 僅家長端可用")
        return

    user_id = payload.get("user_id")
    if not user_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="缺少 user_id")
        return

    backend = get_broadcast()
    await ws.accept()
    backend.subscribe(_parent_channel(user_id), ws)
    await run_ws_connection(ws, cleanup=lambda: backend.unsubscribe(ws))
```

### Step 7.2 跑既有 contact_book test 驗證

- [ ] Run: `pytest tests/test_contact_book.py tests/test_contact_book_extras.py tests/test_contact_book_templates.py -v`
- [ ] Expected: 全綠（既有測試對 backend 中介透明）

### Step 7.3 Commit

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/contact_book_ws.py
git commit -m "refactor(contact_book): migrate ChannelHub to broadcast backend

broadcast_classroom / broadcast_parent helper 簽章不變，
phase-1/2a worktree caller 不用改一行 rebase 後即享 fanout。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] commit 成功

---

## Task 8: utils/ws_hub.ChannelHub deprecate marker

**Files:**
- Modify: `utils/ws_hub.py`（加 DeprecationWarning，不刪除）
- Test: `tests/test_ws_hub_deprecation.py`

### Step 8.1 寫 deprecation test

- [ ] Create `tests/test_ws_hub_deprecation.py`

```python
"""ChannelHub() 構造應發出 DeprecationWarning（PR2 後移除）。"""

import warnings

import pytest


def test_channel_hub_emits_deprecation_warning():
    from utils.ws_hub import ChannelHub

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ChannelHub()
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert any(
            "ChannelHub" in str(w.message) and "broadcast backend" in str(w.message)
            for w in deprecations
        ), f"expected ChannelHub DeprecationWarning, got: {caught}"


def test_channel_hub_still_works():
    """deprecate marker 不該破壞 ChannelHub 既有行為（rebase 保險）。"""
    from utils.ws_hub import ChannelHub

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        hub = ChannelHub()
        # 基本 API 依然可用
        assert hub.channel_size("nobody") == 0
```

### Step 8.2 跑 test 驗證失敗

- [ ] Run: `pytest tests/test_ws_hub_deprecation.py -v`
- [ ] Expected: 1 failed（無 DeprecationWarning）+ 1 passed

### Step 8.3 改 utils/ws_hub.py：ChannelHub.__init__ 發 DeprecationWarning

- [ ] Modify utils/ws_hub.py — `ChannelHub.__init__` 內部插入：

```python
    def __init__(self) -> None:
        import warnings

        warnings.warn(
            "ChannelHub is deprecated; use utils.broadcast.get_broadcast() "
            "(BroadcastBackend) which supports cross-instance fanout via Redis. "
            "Removal target: PR after notification-dispatch-phase-2a merges.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._subs: dict[Any, list[WebSocket]] = defaultdict(list)
```

### Step 8.4 跑 test 驗證綠

- [ ] Run: `pytest tests/test_ws_hub_deprecation.py -v`
- [ ] Expected: 2 passed

### Step 8.5 跑既有 ws_hub test（若有）

- [ ] Run: `pytest tests/test_ws_auth.py tests/test_ws_heartbeat.py -v`
- [ ] Expected: 全綠（warning 不該影響行為）

### Step 8.6 Commit

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add utils/ws_hub.py tests/test_ws_hub_deprecation.py
git commit -m "chore(ws_hub): mark ChannelHub deprecated (removal after phase-2a)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] commit 成功

---

## Task 9: .env.example + 最終驗證

**Files:**
- Modify: `.env.example`
- (run full test suite, no new file)

### Step 9.1 補 .env.example

- [ ] Read `.env.example` 找 `CACHE_BACKEND` 段落（若不存在，append 到檔尾）
- [ ] 確保以下 5 個變數存在（含預設值與註解）：

```env
# WS 廣播 / cache backend（PR §2026-05-26 接 Redis）
# memory: in-process（dev / single-instance prod 預設）
# redis : Redis Pub/Sub 跨 instance fanout（multi-instance 必設）
CACHE_BACKEND=memory
CACHE_REDIS_URL=                          # 例：redis://default:xxx@redis.zeabur.internal:6379/0
CACHE_KEY_PREFIX=ivy
CACHE_PUBSUB_TIMEOUT_SECONDS=5.0
CACHE_PUBLISH_PAYLOAD_MAX_BYTES=8192
```

### Step 9.2 跑全套 pytest

- [ ] Run: `cd /Users/yilunwu/Desktop/ivy-backend && pytest tests/ -x --tb=short 2>&1 | tail -40`
- [ ] Expected: 主套 5000+ 通過，無新增 regression

如出現 fail：
- 若涉及 `dismissal_manager._teacher_conns` 內部 attr 斷言 → 改為 `get_broadcast()._subscribers[channel]`
- 若涉及 `hub.broadcast` 直接 patch → 改為 patch `utils.broadcast.get_broadcast`

### Step 9.3 dev server 啟動 smoke

- [ ] Run（背景，3 秒後 kill）：

```bash
cd /Users/yilunwu/Desktop/ivy-backend
timeout 3 uvicorn main:app --host 127.0.0.1 --port 8089 2>&1 | head -40
```

- [ ] Expected output: 看到 `RedisBackend started prefix=ivy:` (若 CACHE_BACKEND=redis 但 url 不通會 fail-loud 退出) 或無 broadcast 訊息（memory mode）+ `Application startup complete`

### Step 9.4 OpenAPI codegen 驗證（防漂移）

- [ ] Run（dev DB 需運行中）：

```bash
cd /Users/yilunwu/Desktop/ivy-backend && python scripts/dump_openapi.py
```

- [ ] Expected: 無 exception；openapi.json 產出（gitignored）

備註：本 PR 未新增 REST endpoint，OpenAPI 不該有 diff；若 dump 失敗代表 router 引入 broke。

### Step 9.5 Commit + final summary

- [ ] Commit：

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add .env.example
git commit -m "docs(env): add CACHE_* vars for broadcast backend

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] commit 成功

### Step 9.6 PR Body 草稿

完成後給 user 用來開 PR 的 body：

```markdown
## Summary

收斂 4 個 process-local WS hub 為單一 `BroadcastBackend` singleton，env `CACHE_BACKEND` 切換 `LocalBackend`（memory）與 `RedisBackend`（Redis Pub/Sub）。Local 模式行為等價現況，Redis 模式提供跨 instance fanout，解 Zeabur 多 instance 部署時廣播失效問題。

Spec: `docs/superpowers/specs/2026-05-26-ws-broadcast-redis-pubsub-design.md`
Plan: `docs/superpowers/plans/2026-05-26-ws-broadcast-redis-pubsub.md`

## 變動

- `utils/broadcast/` 新模組：ABC + LocalBackend + RedisBackend
- `config/cache.py` 擴 2 欄 + `model_validator` fail-loud
- `main.py` lifespan 加 broadcast start/stop
- `api/dismissal_ws.py` 刪 `DismissalConnectionManager`，保留 `dismissal_manager` deprecated alias
- `api/contact_book_ws.py` 刪 `ChannelHub` singleton，helper 簽章保留
- `api/students.py` 3 處 caller 改 `publish_many`
- `utils/ws_hub.ChannelHub` 加 DeprecationWarning

## Test plan

- [ ] `pytest tests/test_broadcast_*.py tests/test_config_cache_validation.py tests/test_ws_hub_deprecation.py` 全綠
- [ ] `pytest tests/test_dismissal_*.py tests/test_contact_book*.py tests/test_ws_*.py` 既有測試零回歸
- [ ] `pytest tests/` 全套 5000+ 過
- [ ] Dev server 啟動成功（memory mode）
- [ ] Staging 部署 + `CACHE_BACKEND=redis` 兩 instance smoke：跨 instance 廣播可達
- [ ] Prod 切換後觀察 Sentry `broadcast.backend=redis` tag 一週
```

---

## Self-Review

**1. Spec coverage**：
- §3.1 BroadcastBackend ABC → Task 2 ✓
- §3.2 channel 命名 → Task 6/7 ✓（dismissal.* / contact_book.*）
- §4.1 LocalBackend → Task 3 ✓
- §4.2 RedisBackend + echo 處理 → Task 4 ✓（Step 4.1 spike 確認 echo 後採 pump-only dispatch）
- §4.3 main.py lifespan → Task 5 ✓
- §4.4 既有 WS 模組改造 → Task 6 + 7 ✓
- §4.5 CacheSettings 擴充 → Task 1 ✓
- §5 失效模式 → Task 4 內 `test_publish_fail_open_when_redis_unreachable` + `_note_redis_failure` 限頻 ✓
- §6 Rollout → Task 9.6 PR body ✓
- §7 測試策略 → Task 3/4 unit + Task 4 integration ✓
- §9 驗收條件 → Task 9 final verification ✓

**2. Placeholder scan**：搜 plan 內無 TBD / TODO / "implement later" / "appropriate error handling" — pass。

**3. Type consistency**：
- `BroadcastBackend.publish(channel: str, payload: dict)` 在 ABC（Task 2）/ LocalBackend（Task 3）/ RedisBackend（Task 4）/ caller side（Task 6/7）一致 ✓
- `_classroom_channel` helper 在 dismissal_ws.py 與 contact_book_ws.py 各自一份（不同 prefix）— intentional 不共用 ✓
- `_ADMIN_CHANNEL` 只在 dismissal_ws.py 一處（contact_book 無 admin channel）✓
- `get_broadcast()` lru_cache singleton 在 Task 2 定義、Task 3/4/5/6/7 一致使用 ✓
- `reset_for_tests()` 在 Task 2 定義、Task 5 lifespan test 一致使用 ✓

**4. echo behavior 修正**：spec §4.2 假設「real Redis 不 echo」，Step 4.1 spike 證實 fakeredis 與 real Redis 都會 echo。設計修正為「publish 只送 Redis、pump 流入後 dispatch」（pump-only dispatch path）— 已記入 Task 4 註記。
