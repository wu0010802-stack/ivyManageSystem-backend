# WS 連線限制 + LINE Webhook Rate-limit

**日期**：2026-05-28
**狀態**：Design（pending review）
**Scope**：ivy-backend
**前置任務**：P2 audit finding 20-c (WS 無連線數限制) + 20-d (LINE webhook 無 rate-limit)
**相關**：finding 20-d 「replay 防護」部分 **audit 前提錯**：LINE webhook 已有 HMAC signature + ±5min timestamp skew + webhookEventId UNIQUE dedup 三重防護，**本 spec 不重做** replay
**工時估**：1 天

---

## 1. 動機

### 1.1 Finding 20-c：WS 無連線數限制

當前 3 個 WS endpoint（`/api/ws/portal/dismissal-calls`、`/api/ws/admin/dismissal-calls`、`/api/ws/portal/contact-book` 等）在 `ws.accept()` 後直接 `backend.subscribe(channel, ws)`，無 per-user 連線數上限。攻擊者單帳號可開無限 WS 消耗 fd / memory，最終 worker 503。

### 1.2 Finding 20-d：LINE webhook 無 rate-limit

`POST /api/line/webhook` 入口完全無 rate-limit；雖有 HMAC signature 拒未授權，但 LINE Platform 本身 retry 過量 / 將來 channel secret 洩漏 / debug 階段 flood 都會直接灌進 DB（webhookEventId dedup 雖檔重複但前置 query 已花成本）。

### 1.3 不做什麼（YAGNI）

- **不重做 LINE webhook replay 防護**：已落地（line_webhook.py:47-52 HMAC + line:89-97 ±5min skew + line:109-130 webhookEventId UNIQUE dedup）
- **不導 cross-instance WS 連線計數**：WS 是 stateful per-connection，本來就 instance-sticky；per-instance limit = total / instance 數 已足夠
- **不加 env override `WS_MAX_CONN_PER_USER`**：YAGNI；hard-code 8 簡單；prod 真有 UX 反饋再加 config
- **不對 LINE webhook 加 per-source_user_id rate-limit**：per-channel 1000/5min 已能擋 flood；per-user 對「家長亂點」場景 30/5min 是 over-engineering（reply 一次幾秒，30 次 = 15min 互動 = 正常使用 unlikely 觸發）

---

## 2. 範圍與整體架構

```
ivy-backend/
├── utils/
│   └── ws_connection_limiter.py     ← 新檔：WS 連線計數 + assert_under_limit
├── api/
│   ├── dismissal_ws.py              ← 修改：2 處 ws.accept() 前接 limiter
│   ├── contact_book_ws.py           ← 修改：2 處 ws.accept() 前接 limiter
│   ├── inbox_ws.py                  ← 修改：若 Phase 3 endpoint 已落地則接 limiter；未落地 noop
│   └── line_webhook.py              ← 修改：endpoint top 加 PostgresLimiter check
└── tests/
    ├── test_ws_connection_limiter.py    ← 新檔
    ├── test_dismissal_ws_limit.py       ← 新檔：endpoint 整合測（拒 9th 條）
    └── test_line_webhook_rate_limit.py  ← 新檔
```

---

## 3. WS 連線限制 Helper

### 3.1 新檔 `utils/ws_connection_limiter.py`

```python
"""WS 連線數上限 helper — per-user across all WS endpoints。

Instance-local（per-process dict）。Multi-instance 部署時每 instance 各維護
8 條上限，total = instance × 8。WS 連線本來就 instance-sticky（reverse proxy
hash），這個 trade-off 可接受。

威脅：單一 authenticated user 開無限 WS 耗 worker fd / memory → 503 全站。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Hard-code 8 條/user。YAGNI env override；prod 真有反饋再加 config。
WS_MAX_CONN_PER_USER = 8

# user_id → list[WebSocket]
_active_ws: dict[int, list[WebSocket]] = defaultdict(list)


class WSConnectionLimitExceeded(Exception):
    """user 已達 WS 連線上限。caller 應 close ws (code=1008)。"""


def register(user_id: int, ws: WebSocket) -> None:
    """新增一個 active WS 進 user 計數。

    必須在 ws.accept() 之後、subscribe 之前呼叫；若 assert_under_limit
    通過後再 register 即可。
    """
    _active_ws[user_id].append(ws)


def unregister(ws: WebSocket) -> None:
    """連線結束 cleanup。idempotent（找不到 user_id 即 noop）。

    caller 應在 finally / run_ws_connection 的 cleanup 接這個。
    """
    for user_id, ws_list in list(_active_ws.items()):
        if ws in ws_list:
            ws_list.remove(ws)
            if not ws_list:
                _active_ws.pop(user_id, None)
            return


def count(user_id: int) -> int:
    """目前該 user 的 active WS 數。"""
    return len(_active_ws.get(user_id, []))


def assert_under_limit(user_id: int) -> None:
    """檢查該 user 未超上限；超則 raise WSConnectionLimitExceeded。

    呼叫順序：
        try:
            assert_under_limit(user_id)
        except WSConnectionLimitExceeded:
            await ws.close(code=1008, reason="ws_connection_limit_exceeded")
            return
        await ws.accept()
        register(user_id, ws)
        # ... do work
        # finally: unregister(ws)
    """
    if count(user_id) >= WS_MAX_CONN_PER_USER:
        logger.warning(
            "ws_connection_limit_exceeded user_id=%s current=%d max=%d",
            user_id,
            count(user_id),
            WS_MAX_CONN_PER_USER,
        )
        raise WSConnectionLimitExceeded()


def reset_for_tests() -> None:
    """清掉 in-memory state；只用於 tests / dev 重啟模擬。"""
    _active_ws.clear()
```

### 3.2 設計理由

- **per-user across endpoints**：同一 user_id 跨 inbox + dismissal + contact_book 共用 8 條上限。防 fd 耗盡。
- **拒第 N+1（不踢舊）**：close(code=1008, reason="ws_connection_limit_exceeded")。Client UX = 「新 tab 連不上」。LRU 踢舊較複雜（要 track timestamp），YAGNI。
- **Instance-local**：per-process dict，no Redis。WS 本來 instance-sticky；multi-instance total limit = instance × 8。
- **`unregister` idempotent**：double-call 安全（cleanup 在 except 路徑可能跑兩次）。
- **`reset_for_tests`**：對齊既有 `utils/rate_limit.py` test fixture pattern。

---

## 4. WS Endpoint 接入

### 4.1 既有 3 個 endpoint 模式

`api/dismissal_ws.py`、`api/contact_book_ws.py` 各有 2 個 `@ws_router.websocket(...)`（admin + portal）。每個都長這樣：

```python
@ws_router.websocket("/api/ws/portal/contact-book")
async def portal_contact_book_ws(ws: WebSocket, ...):
    # ... auth payload extract ...
    user_id = payload.get("user_id")
    if not user_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="缺少 user_id")
        return

    await ws.accept()
    backend.subscribe(_parent_channel(user_id), ws)
    await run_ws_connection(ws, cleanup=lambda: backend.unsubscribe(ws))
```

### 4.2 改造 pattern（每 endpoint 共用）

```python
from utils.ws_connection_limiter import (
    WSConnectionLimitExceeded,
    assert_under_limit,
    register,
    unregister,
)

@ws_router.websocket("/api/ws/portal/contact-book")
async def portal_contact_book_ws(ws: WebSocket, ...):
    # ... auth payload extract → user_id ...
    if not user_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="缺少 user_id")
        return

    # 新增：連線數上限檢查（必須在 accept 之前）
    try:
        assert_under_limit(user_id)
    except WSConnectionLimitExceeded:
        await ws.close(code=1008, reason="ws_connection_limit_exceeded")
        return

    await ws.accept()
    register(user_id, ws)
    backend.subscribe(_parent_channel(user_id), ws)

    def cleanup():
        backend.unsubscribe(ws)
        unregister(ws)

    await run_ws_connection(ws, cleanup=cleanup)
```

**5 個 endpoint 都套同 pattern**：
- `api/dismissal_ws.py:101` portal dismissal（auth path: classroom_id → 從 JWT user_id）
- `api/dismissal_ws.py:149` admin dismissal（auth path: admin user_id）
- `api/contact_book_ws.py:62` portal contact_book classroom（teacher user_id）
- `api/contact_book_ws.py:108` parent contact_book（parent user_id）
- `api/inbox_ws.py` 若 Phase 3 endpoint 已落地：接同 pattern；若仍 skeleton 則本 PR 不動

### 4.3 設計理由

- **assert 必在 accept 前**：先 close 不開 TCP layer 不會消耗 fd
- **register 在 accept 後**：accept 失敗就不該計入
- **cleanup 含 unregister**：與 backend.unsubscribe 一起 unregister，保證連線結束就釋放

---

## 5. LINE Webhook Rate-limit

### 5.1 設計

**接入點**：`api/line_webhook.py:line_webhook()` 內部 top（在 signature verify 之後、parse events 之前）。

**Limiter**：重用既有 `utils.rate_limit.create_limiter(max_calls=1000, window_seconds=300, name="line_webhook")`，走 RATE_LIMIT_BACKEND env 控制（dev memory / prod postgres）。

**Key**：`"channel"`（hard-code 字串；本系統假設單一 LINE channel，無 multi-channel concept）。

### 5.2 diff pattern

```python
from utils.rate_limit import create_limiter

# Module-level singleton（與既有 limiter pattern 一致）
_LINE_WEBHOOK_LIMITER = create_limiter(
    max_calls=1000,
    window_seconds=300,
    name="line_webhook",
    error_detail="LINE webhook rate-limit exceeded",
)


@router.post("/webhook")
async def line_webhook(body: bytes = Depends(verify_line_signature)):
    # 新增：rate-limit check
    _LINE_WEBHOOK_LIMITER.check("channel")  # 超限拋 HTTPException(429)

    import json
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="無效的 JSON")
    # ... rest unchanged ...
```

### 5.3 設計理由

- **rate-limit 在 signature verify 後**：先驗 HMAC 才計入 limit，攻擊者送 garbage 不消耗 limit budget
- **per-channel hard-code**：1 channel system；future multi-channel 可以拿 source.userId / channel-secret hash 當 key
- **threshold 1000/5min**：正常 LINE webhook events 估每月幾千 events，5min 內 1000 events = peak burst 上限；攻擊 flood 立刻 429
- **fail-open**：`PostgresLimiter.check` 內 DB 失敗即 fail-open（不擋 webhook）。sub-project #2 (PR #46) 已 instrument 該 except 點接 Sentry capture_fail_open；無論 #3 / #2 哪個先 merge，本 PR 不需動 limiter 內部，merge 兩 PR 後自動繼承 observability

---

## 6. 測試

### 6.1 新檔 `tests/test_ws_connection_limiter.py`

```python
"""WSConnectionLimiter unit tests。"""
from unittest.mock import MagicMock

import pytest

from utils.ws_connection_limiter import (
    WS_MAX_CONN_PER_USER,
    WSConnectionLimitExceeded,
    assert_under_limit,
    count,
    register,
    reset_for_tests,
    unregister,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()


def test_assert_under_limit_passes_below_max():
    user_id = 42
    for _ in range(WS_MAX_CONN_PER_USER - 1):
        register(user_id, MagicMock())
    # 還差 1 個達上限，assert 應通過
    assert_under_limit(user_id)


def test_assert_under_limit_raises_at_max():
    user_id = 42
    for _ in range(WS_MAX_CONN_PER_USER):
        register(user_id, MagicMock())
    with pytest.raises(WSConnectionLimitExceeded):
        assert_under_limit(user_id)


def test_unregister_decrements_count():
    user_id = 42
    ws = MagicMock()
    register(user_id, ws)
    assert count(user_id) == 1
    unregister(ws)
    assert count(user_id) == 0


def test_unregister_idempotent():
    """double-call unregister 不 raise。"""
    user_id = 42
    ws = MagicMock()
    register(user_id, ws)
    unregister(ws)
    unregister(ws)  # second call no-op
    assert count(user_id) == 0


def test_unregister_unknown_ws_noop():
    """unregister 從未 register 的 ws 不 raise。"""
    unregister(MagicMock())  # 無 raise 即視為 pass


def test_isolation_between_users():
    """user A 達上限不影響 user B。"""
    for _ in range(WS_MAX_CONN_PER_USER):
        register(1, MagicMock())
    # user 2 仍可 register
    assert_under_limit(2)
    register(2, MagicMock())
    assert count(2) == 1
```

### 6.2 新檔 `tests/test_line_webhook_rate_limit.py`

```python
"""LINE webhook rate-limit 接入測試。

Memory limiter backend（RATE_LIMIT_BACKEND=memory）下，連續 1001 次 webhook
call 應第 1001 次拿 429。
"""
import hashlib
import hmac
import json
import base64

import pytest
from fastapi.testclient import TestClient


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_line_webhook_rate_limit_exceeds_429(monkeypatch):
    """over 1000 / 5min 應 429。"""
    # 設定 memory backend + 小 threshold（避免測 1000 次）
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")

    # 用 monkeypatch 把 _LINE_WEBHOOK_LIMITER 換成低 threshold
    from utils.rate_limit import create_limiter

    test_limiter = create_limiter(
        max_calls=3, window_seconds=60, name="line_webhook_test"
    )
    import api.line_webhook as lw

    monkeypatch.setattr(lw, "_LINE_WEBHOOK_LIMITER", test_limiter)

    # mock _line_service + channel_secret
    class FakeService:
        _channel_secret = "test_secret"

    monkeypatch.setattr(lw, "_line_service", FakeService())

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(lw.router)
    client = TestClient(app)

    body = json.dumps({"events": []}).encode()
    sig = _signature("test_secret", body)

    # 前 3 次 pass
    for i in range(3):
        r = client.post(
            "/api/line/webhook", content=body, headers={"X-Line-Signature": sig}
        )
        assert r.status_code == 200, f"第 {i + 1} 次應 pass"

    # 第 4 次超限應 429
    r = client.post(
        "/api/line/webhook", content=body, headers={"X-Line-Signature": sig}
    )
    assert r.status_code == 429
```

### 6.3 新檔 `tests/test_dismissal_ws_limit.py`

WS endpoint 整合測（顯示 9th 連線被拒）— 範例（implementer 可調整 fixture 以符既有 conftest pattern）：

```python
"""dismissal_ws 連線上限整合測。"""
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.ws_connection_limiter import WS_MAX_CONN_PER_USER, reset_for_tests


def test_dismissal_ws_rejects_over_limit(monkeypatch):
    """同 user_id 第 (WS_MAX_CONN_PER_USER+1) 條 ws 應被 close(1008)。"""
    reset_for_tests()
    # ... build app + auth fixture ...
    # 連 WS_MAX_CONN_PER_USER 次 OK
    # 第 N+1 次：connect 後立即被 close 1008
```

⚠️ 此 endpoint 整合測對 WebSocket 連線測試需要 conftest fixture 支援（既有 `tests/test_dismissal_calls.py` / `tests/test_ws_heartbeat.py` 已有 pattern，implementer 參考既有 fixture）。

### 6.4 既有 WS test regression

`tests/test_dismissal_calls.py` + `tests/test_ws_heartbeat.py` 既有測試保留不動；本 PR 只新加 limiter，既有測試 fixture 不會超 8 條，零 regression。

---

## 7. 行為變更與 User 影響

| 場景 | 既有 | 新 |
|---|---|---|
| HR 開太多 tab（>8 個含後台 + portal） | 全部連線正常 | 第 9 個 tab 連 WS 失敗 + 紅色 toast「連線數已達上限」 |
| LINE 大量 retry 同 webhook event | 全部寫 dedup query 後 skip | 1000 次內同上；1001 次起 429 |
| 攻擊者單帳號 flood WS | fd / memory 慢慢耗盡 → worker 503 | 第 9 條起 close(1008)，無 fd 耗盡 |
| 攻擊者 channel secret 洩漏 flood webhook | 全部進 DB dedup query | 1001/5min 即 429 |

**前端 UX 影響：** WS 被 close(1008) 時 client 需顯示「連線數已達上限」toast 而非靜默 reconnect loop。**本 PR 不改前端**；前端 follow-up 屬 sub-project（前端 onmessage close handler）。本 PR 接受短期 UX：close 1008 → client onclose handler 觸發 reconnect → 又被拒（直到 user 關閉其他 tab）。

---

## 8. Prerequisites

無 user 手動操作。

---

## 9. Out of Scope（follow-up）

| Follow-up | 屬性 | 何時做 |
|---|---|---|
| 前端 WS close(1008) 顯示 toast + stop reconnect loop | UX 改善 | 本 PR merge 後一週內 sprint |
| `WS_MAX_CONN_PER_USER` env override | config 彈性 | prod 反饋上限不夠用時 |
| Cross-instance WS counter（Redis） | scalability | 若導入 multi-instance + per-instance counter 不夠 |
| LINE webhook per-source_user_id rate-limit | 細粒度 | 若 channel-level limit 不夠 |
| LINE webhook log retention 政策 | compliance | 法律需求變更時 |

---

## 10. 風險與回退

### 10.1 主要風險

- **WS 上限太低**：8 條對單 user 開多 tab 可能不夠。Mitigation：observability — 觀察 close(1008) 頻率，若多則調 16 或 32
- **LINE webhook 1000/5min 太緊**：正常使用估遠低於這個值，但若有 follow event 大量觸發（家長集體加 LINE）可能撞線。Mitigation：監控 Sentry 收到 429，立刻調為 5000/5min
- **WS limiter reset 在 test fixture 漏接**：multi-test 共用 module state 累積導致後續 test 假性超限。Mitigation：`reset_for_tests` 在 conftest autouse fixture

### 10.2 回退方式

- **完整回退**：revert PR
- **暫關 WS limit**：`WS_MAX_CONN_PER_USER = 9999` 一行改 + restart
- **暫關 LINE rate-limit**：註解掉 `_LINE_WEBHOOK_LIMITER.check("channel")` 一行 + restart

---

## 11. 預估與分工

- **規模**：1 新檔 (`utils/ws_connection_limiter.py` ~75 行) + 3-4 修改檔（line_webhook.py / dismissal_ws.py / contact_book_ws.py /（optional）inbox_ws.py）+ 3 新 test 檔
- **工時**：1 天（含 spec / plan / commit / push / PR）
- **PR 數**：1（backend only）
- **依賴**：無
- **block 後續 sub-project？** 不 block
