# WS 連線限制 + LINE Webhook Rate-limit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 加入 per-user WS 連線上限（8 條跨 endpoint，拒第 N+1 條 close(1008)）+ LINE webhook per-channel rate-limit（1000 events/5min）。

**Architecture:** 新 `utils/ws_connection_limiter.py` 提供 in-memory per-user counter（`assert_under_limit` / `register` / `unregister` / `count` / `reset_for_tests`）；4 個 WS endpoint 統一 pattern 在 `ws.accept()` 前 check。LINE webhook 重用既有 `utils.rate_limit.create_limiter` factory，module-level singleton 在 signature verify 後、parse events 前 check。

**Tech Stack:** Python 3.9 / FastAPI WebSocket / SQLAlchemy / pytest

**Spec:** `docs/superpowers/specs/2026-05-28-ws-line-webhook-rate-limit-design.md`

**重要 context：**
- 當前 git branch 應為 `feat/ws-line-webhook-rate-limit-2026-05-28-backend`（spec commit `7559512` 已在上）。執行前 `git branch --show-current` 確認
- Branch base 是 origin/main，**不含** sub-project #1 (dependabot PR #41/#42) 或 #2 (signed URL + fail-open PR #46) 的改動。本 plan 全程 self-contained，不依賴 #1 / #2 merge
- spec §5.3 提到 LINE limiter fail-open 觀察性會在 #2 merge 後自動繼承（無需本 PR 動）
- Backend working tree 可能有 user 並行 WIP（`pyproject.toml` / `uv.lock` 等）。**不 stash、不 `git add -A`**，只 add 本 plan 列具體檔案
- 4 個 WS endpoint user identity 來源都是 `verify_ws_token(token)` 回的 `payload`；payload 含 `user_id` claim（既有 JWT 標準 claim，4 個 endpoint 都可取）

---

## File Structure

```
ivy-backend/
├── utils/
│   └── ws_connection_limiter.py             ← Task 1 (Create ~75 行)
├── api/
│   ├── line_webhook.py                      ← Task 2 (Modify: add module-level limiter + check call)
│   ├── dismissal_ws.py                      ← Task 3 (Modify portal) + Task 4 (Modify admin)
│   └── contact_book_ws.py                   ← Task 5 (Modify portal) + Task 6 (Modify parent)
└── tests/
    ├── test_ws_connection_limiter.py        ← Task 1 (Create 6 tests)
    └── test_line_webhook_rate_limit.py      ← Task 2 (Create 1 integration test)
```

**Out of scope (per spec §6.3)：** WS endpoint full integration test（拒第 9 條連線）需要 conftest fixture 跑 WebSocket TestClient。既有 `tests/test_dismissal_calls.py` / `tests/test_ws_heartbeat.py` 有 pattern 但複雜。本 plan **不做** endpoint integration test；helper unit test 已驗證核心邏輯，endpoint 接入是 mechanical pattern application，merge 後 user 可手測。

---

## Task 1: `WSConnectionLimiter` Helper (TDD)

**Files:**
- Create: `ivy-backend/utils/ws_connection_limiter.py`
- Test: `ivy-backend/tests/test_ws_connection_limiter.py`

- [ ] **Step 1: Confirm on correct branch**

```bash
cd ~/Desktop/ivy-backend
git branch --show-current
```

Expected: `feat/ws-line-webhook-rate-limit-2026-05-28-backend`

- [ ] **Step 2: Write the failing test file**

Create `~/Desktop/ivy-backend/tests/test_ws_connection_limiter.py`:

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

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_ws_connection_limiter.py -v 2>&1 | tail -10
```

Expected: 6 tests fail (collection error) — `ModuleNotFoundError: No module named 'utils.ws_connection_limiter'`

- [ ] **Step 4: Create the helper**

Create `~/Desktop/ivy-backend/utils/ws_connection_limiter.py`:

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

    必須在 ws.accept() 之後、subscribe 之前呼叫；assert_under_limit
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
        # cleanup must call unregister(ws)
    """
    current = count(user_id)
    if current >= WS_MAX_CONN_PER_USER:
        logger.warning(
            "ws_connection_limit_exceeded user_id=%s current=%d max=%d",
            user_id,
            current,
            WS_MAX_CONN_PER_USER,
        )
        raise WSConnectionLimitExceeded()


def reset_for_tests() -> None:
    """清掉 in-memory state；只用於 tests / dev 重啟模擬。"""
    _active_ws.clear()
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_ws_connection_limiter.py -v 2>&1 | tail -15
```

Expected: 6 passed in <2s

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add utils/ws_connection_limiter.py tests/test_ws_connection_limiter.py
git status --short
```

Expected: 2 行 `A  utils/ws_connection_limiter.py` + `A  tests/test_ws_connection_limiter.py`；user WIP 仍 unstaged。

```bash
git commit -m "$(cat <<'EOF'
feat(utils): 加入 WSConnectionLimiter helper

per-user across WS endpoints 連線上限 8 條（in-memory，per-process）。
assert_under_limit 超則 raise WSConnectionLimitExceeded；caller 應
close(code=1008)。register/unregister idempotent，避免 cleanup 多次拋。

防單帳號開無限 WS 耗 worker fd / memory。Multi-instance 時 total =
instance × 8（WS 本來 sticky）。

Refs: docs/superpowers/specs/2026-05-28-ws-line-webhook-rate-limit-design.md §3

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: LINE Webhook Rate-limit (TDD)

**Files:**
- Modify: `ivy-backend/api/line_webhook.py` (add import + module-level limiter + check call)
- Test: `ivy-backend/tests/test_line_webhook_rate_limit.py` (new)

- [ ] **Step 1: Write the failing integration test**

Create `~/Desktop/ivy-backend/tests/test_line_webhook_rate_limit.py`:

```python
"""LINE webhook rate-limit 接入測試。

Memory limiter backend（RATE_LIMIT_BACKEND=memory）下，超過 N 次同 channel
key 後第 N+1 次拿 429。為避免測 1000 次，monkeypatch 模組 singleton 換成
低 threshold。
"""
import base64
import hashlib
import hmac
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_line_webhook_rate_limit_exceeds_429(monkeypatch):
    """前 3 次 pass，第 4 次超 (limiter max=3) 應 429。"""
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")

    # 替換 module singleton 成低 threshold（避免測 1000 次）
    from utils.rate_limit import create_limiter

    test_limiter = create_limiter(
        max_calls=3,
        window_seconds=60,
        name="line_webhook_test",
        error_detail="LINE webhook rate-limit exceeded (test)",
    )
    import api.line_webhook as lw

    monkeypatch.setattr(lw, "_LINE_WEBHOOK_LIMITER", test_limiter)

    # mock _line_service + channel_secret 通過 signature verify
    class FakeService:
        _channel_secret = "test_secret"

    monkeypatch.setattr(lw, "_line_service", FakeService())

    app = FastAPI()
    app.include_router(lw.router)
    client = TestClient(app)

    body = json.dumps({"events": []}).encode()
    sig = _signature("test_secret", body)

    # 前 3 次 pass (events=[] 不 dispatch)
    for i in range(3):
        r = client.post(
            "/api/line/webhook",
            content=body,
            headers={"X-Line-Signature": sig},
        )
        assert r.status_code == 200, f"第 {i + 1} 次應 pass，得 {r.status_code}"

    # 第 4 次超限應 429
    r = client.post(
        "/api/line/webhook",
        content=body,
        headers={"X-Line-Signature": sig},
    )
    assert r.status_code == 429
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_line_webhook_rate_limit.py -v 2>&1 | tail -10
```

Expected: FAIL — `monkeypatch.setattr(lw, "_LINE_WEBHOOK_LIMITER", ...)` raises `AttributeError: module 'api.line_webhook' has no attribute '_LINE_WEBHOOK_LIMITER'`

- [ ] **Step 3: Read api/line_webhook.py 確認 import + endpoint 結構**

```bash
sed -n '1,20p' ~/Desktop/ivy-backend/api/line_webhook.py
sed -n '56,80p' ~/Desktop/ivy-backend/api/line_webhook.py
```

紀錄：
- import 區末尾（決定加 `from utils.rate_limit import create_limiter` 的位置）
- `@router.post("/webhook")` decorator 行號
- `async def line_webhook(...)` body 第一行（決定插 limiter check 的位置）

- [ ] **Step 4: Add module-level limiter singleton + import**

Use Edit on `~/Desktop/ivy-backend/api/line_webhook.py`：

加 import（在既有 `from models.database import get_session` 後）：
- old: `from models.database import get_session`
- new:
```
from models.database import get_session
from utils.rate_limit import create_limiter
```

加 module-level singleton（在 `_line_service = None` 後）：
- old: `_line_service = None`
- new:
```
_line_service = None

# Rate-limit: per-channel 1000 events / 5min（spec §5）
_LINE_WEBHOOK_LIMITER = create_limiter(
    max_calls=1000,
    window_seconds=300,
    name="line_webhook",
    error_detail="LINE webhook rate-limit exceeded",
)
```

- [ ] **Step 5: Add check call in endpoint body**

Use Edit on `~/Desktop/ivy-backend/api/line_webhook.py`：

找 endpoint body 第一段 `import json; try: payload = json.loads(body)`：
- old:
```
    import json

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="無效的 JSON")
```
- new:
```
    # Rate-limit (spec §5): 在 signature verify 後、parse events 前 check
    _LINE_WEBHOOK_LIMITER.check("channel")

    import json

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="無效的 JSON")
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_line_webhook_rate_limit.py -v 2>&1 | tail -10
```

Expected: 1 passed

- [ ] **Step 7: Run existing line_webhook tests for regression**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_line_webhook.py tests/test_line_webhook_v2.py -v 2>&1 | tail -15
```

Expected: 既有 line_webhook test 全 pass（events=[] body 不會撞 1000 上限，limiter 不影響）。

- [ ] **Step 8: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/line_webhook.py tests/test_line_webhook_rate_limit.py
git status --short
```

Expected: `M  api/line_webhook.py` + `A  tests/test_line_webhook_rate_limit.py`。

```bash
git commit -m "$(cat <<'EOF'
feat(line_webhook): per-channel rate-limit 1000 events / 5min

新 module-level _LINE_WEBHOOK_LIMITER 用 create_limiter factory
（受 RATE_LIMIT_BACKEND env 控制 memory/postgres）。在 signature
verify 後、parse events 前 check("channel")；超限拋 429。

防 LINE Platform 過量 retry / channel secret 洩漏 flood。

Refs: docs/superpowers/specs/2026-05-28-ws-line-webhook-rate-limit-design.md §5

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `portal_dismissal_ws` 接入 Limiter

**Files:**
- Modify: `ivy-backend/api/dismissal_ws.py:101-146` (portal_dismissal_ws)

- [ ] **Step 1: Read endpoint 既有結構**

```bash
sed -n '95,150p' ~/Desktop/ivy-backend/api/dismissal_ws.py
```

確認：
- payload extract 處（`employee_id = payload.get("employee_id")` line ~121）
- `await ws.accept()` 行（line ~138 + line ~142）
- cleanup callback 位置（line ~139, 146）

⚠️ **注意**：此 endpoint 有兩個 `await ws.accept()` 分支（line ~138 是 no-classroom path，line ~142 是 has-classroom path）。**兩個 accept 都需在前面加 limit check**；register/cleanup 也兩個分支都要接。

- [ ] **Step 2: Add import**

Use Edit on `~/Desktop/ivy-backend/api/dismissal_ws.py`：

找既有最後一個 `from utils.X import ...` 行（若無則找 stdlib import 區末尾），加：
```
from utils.ws_connection_limiter import (
    WSConnectionLimitExceeded,
    assert_under_limit,
    register,
    unregister,
)
```

具體位置：在既有 `from utils.broadcast import get_broadcast` 之後（若存在）；否則在 imports 區末尾。Read 確認後決定。

- [ ] **Step 3: Modify portal_dismissal_ws — extract user_id + assert + register + cleanup**

⚠️ **此 endpoint 既有沒 extract `user_id`**（只用 `employee_id`）。本 plan 為了 limiter 統一 key，從 payload 取 `user_id`（既有 JWT 標準 claim）：

Use Edit on portal_dismissal_ws：

- old:
```
    employee_id = payload.get("employee_id")
    if not employee_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="此帳號無對應教師身分")
        return
```
- new:
```
    employee_id = payload.get("employee_id")
    if not employee_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="此帳號無對應教師身分")
        return

    user_id = payload.get("user_id")
    if not user_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="缺少 user_id")
        return

    # WS 連線上限檢查（必須在 accept 之前）
    try:
        assert_under_limit(user_id)
    except WSConnectionLimitExceeded:
        await ws.close(code=1008, reason="ws_connection_limit_exceeded")
        return
```

修 no-classroom path 的 accept block：

- old:
```
    classroom_ids = _get_teacher_classroom_ids(employee_id)
    backend = get_broadcast()
    if not classroom_ids:
        # 若老師尚未分班，仍允許連線但不會收到任何班級事件
        await ws.accept()
        await _run_connection(ws)
        return
```
- new:
```
    classroom_ids = _get_teacher_classroom_ids(employee_id)
    backend = get_broadcast()
    if not classroom_ids:
        # 若老師尚未分班，仍允許連線但不會收到任何班級事件
        await ws.accept()
        register(user_id, ws)
        await _run_connection(ws, cleanup=lambda: unregister(ws))
        return
```

修 has-classroom path 的 accept block：

- old:
```
    await ws.accept()
    for cid in classroom_ids:
        backend.subscribe(_classroom_channel(cid), ws)
    logger.info("教師 WS 已連線，班級 IDs: %s", classroom_ids)
    await _run_connection(ws, cleanup=lambda: backend.unsubscribe(ws))
```
- new:
```
    await ws.accept()
    register(user_id, ws)
    for cid in classroom_ids:
        backend.subscribe(_classroom_channel(cid), ws)
    logger.info("教師 WS 已連線，班級 IDs: %s", classroom_ids)

    def _cleanup():
        backend.unsubscribe(ws)
        unregister(ws)

    await _run_connection(ws, cleanup=_cleanup)
```

- [ ] **Step 4: Verify no syntax error**

```bash
cd ~/Desktop/ivy-backend
python3 -c "import api.dismissal_ws" && echo "import OK"
```

Expected: `import OK`

- [ ] **Step 5: Run existing dismissal tests**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_dismissal_calls.py tests/test_ws_heartbeat.py -v 2>&1 | tail -15
```

Expected: 全 pass（既有 test 不會超 8 條）。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/dismissal_ws.py
git status --short
```

Expected: `M  api/dismissal_ws.py`。

```bash
git commit -m "$(cat <<'EOF'
feat(ws): portal_dismissal_ws 接 WS connection limiter

extract user_id from JWT payload，accept 前 assert_under_limit
（超則 close 1008）；accept 後 register，cleanup 加 unregister。
No-classroom + has-classroom 兩 path 都接。

Refs: docs/superpowers/specs/2026-05-28-ws-line-webhook-rate-limit-design.md §4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `admin_dismissal_ws` 接入 Limiter

**Files:**
- Modify: `ivy-backend/api/dismissal_ws.py:149-182` (admin_dismissal_ws)

- [ ] **Step 1: Confirm Task 3 import 已加（避免重複）**

```bash
cd ~/Desktop/ivy-backend
grep "ws_connection_limiter" api/dismissal_ws.py
```

Expected: 已有 import line（Task 3 加過）。如無：Task 3 出錯，回去檢查。

- [ ] **Step 2: Modify admin_dismissal_ws — extract user_id + assert + register + cleanup**

Use Edit on `~/Desktop/ivy-backend/api/dismissal_ws.py`：

找 `if not has_permission(...) return` block 後到 `await ws.accept()` 之間。在 `backend = get_broadcast()` 前加 user_id extract + limit check：

- old:
```
    if not has_permission(payload.get("permission_names"), Permission.STUDENTS_READ):
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="權限不足，需要學生讀取權限")
        return

    backend = get_broadcast()
    await ws.accept()
    backend.subscribe(_ADMIN_CHANNEL, ws)
    logger.info("管理端 WS 已連線")
    await _run_connection(ws, cleanup=lambda: backend.unsubscribe(ws))
```
- new:
```
    if not has_permission(payload.get("permission_names"), Permission.STUDENTS_READ):
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="權限不足，需要學生讀取權限")
        return

    user_id = payload.get("user_id")
    if not user_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="缺少 user_id")
        return

    try:
        assert_under_limit(user_id)
    except WSConnectionLimitExceeded:
        await ws.close(code=1008, reason="ws_connection_limit_exceeded")
        return

    backend = get_broadcast()
    await ws.accept()
    register(user_id, ws)
    backend.subscribe(_ADMIN_CHANNEL, ws)
    logger.info("管理端 WS 已連線")

    def _cleanup():
        backend.unsubscribe(ws)
        unregister(ws)

    await _run_connection(ws, cleanup=_cleanup)
```

- [ ] **Step 3: Verify no syntax error + run existing tests**

```bash
cd ~/Desktop/ivy-backend
python3 -c "import api.dismissal_ws" && echo "import OK"
python3 -m pytest tests/test_dismissal_calls.py tests/test_ws_heartbeat.py -v 2>&1 | tail -10
```

Expected: import OK + 全 pass。

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/dismissal_ws.py
git status --short
```

Expected: `M  api/dismissal_ws.py`。

```bash
git commit -m "$(cat <<'EOF'
feat(ws): admin_dismissal_ws 接 WS connection limiter

extract user_id + assert_under_limit 超則 close 1008；register +
cleanup unregister。

Refs: docs/superpowers/specs/2026-05-28-ws-line-webhook-rate-limit-design.md §4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `portal_contact_book_ws` 接入 Limiter

**Files:**
- Modify: `ivy-backend/api/contact_book_ws.py:62-105` (portal_contact_book_ws)

- [ ] **Step 1: Add import**

Use Edit on `~/Desktop/ivy-backend/api/contact_book_ws.py`：

找既有 utils import 區末尾，加：
```
from utils.ws_connection_limiter import (
    WSConnectionLimitExceeded,
    assert_under_limit,
    register,
    unregister,
)
```

- [ ] **Step 2: Modify portal_contact_book_ws**

Use Edit on `~/Desktop/ivy-backend/api/contact_book_ws.py`：

找 permission check 後到 accept 前：

- old:
```
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
```
- new:
```
    user_id = payload.get("user_id")
    if not user_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="缺少 user_id")
        return

    try:
        assert_under_limit(user_id)
    except WSConnectionLimitExceeded:
        await ws.close(code=1008, reason="ws_connection_limit_exceeded")
        return

    classroom_ids: list[int] = []
    employee_id = payload.get("employee_id")
    if role == "teacher" and employee_id:
        classroom_ids = _get_teacher_classroom_ids(employee_id)

    backend = get_broadcast()
    await ws.accept()
    register(user_id, ws)
    if classroom_ids:
        for cid in classroom_ids:
            backend.subscribe(_classroom_channel(cid), ws)

    def _cleanup():
        backend.unsubscribe(ws)
        unregister(ws)

    await run_ws_connection(ws, cleanup=_cleanup)
```

- [ ] **Step 3: Verify + commit**

```bash
cd ~/Desktop/ivy-backend
python3 -c "import api.contact_book_ws" && echo "import OK"
git add api/contact_book_ws.py
git status --short
```

Expected: `M  api/contact_book_ws.py`。

```bash
git commit -m "$(cat <<'EOF'
feat(ws): portal_contact_book_ws 接 WS connection limiter

extract user_id + assert_under_limit 超則 close 1008；register +
cleanup unregister。

Refs: docs/superpowers/specs/2026-05-28-ws-line-webhook-rate-limit-design.md §4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `parent_contact_book_ws` 接入 Limiter

**Files:**
- Modify: `ivy-backend/api/contact_book_ws.py:108-138` (parent_contact_book_ws)

- [ ] **Step 1: Modify parent_contact_book_ws**

⚠️ 此 endpoint **既有已 extract `user_id`** (line 130)，只要在現有 `user_id` 守衛後加 limit check + accept 後 register + cleanup。

Use Edit on `~/Desktop/ivy-backend/api/contact_book_ws.py`：

- old:
```
    user_id = payload.get("user_id")
    if not user_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="缺少 user_id")
        return

    backend = get_broadcast()
    await ws.accept()
    backend.subscribe(_parent_channel(user_id), ws)
    await run_ws_connection(ws, cleanup=lambda: backend.unsubscribe(ws))
```
- new:
```
    user_id = payload.get("user_id")
    if not user_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="缺少 user_id")
        return

    try:
        assert_under_limit(user_id)
    except WSConnectionLimitExceeded:
        await ws.close(code=1008, reason="ws_connection_limit_exceeded")
        return

    backend = get_broadcast()
    await ws.accept()
    register(user_id, ws)
    backend.subscribe(_parent_channel(user_id), ws)

    def _cleanup():
        backend.unsubscribe(ws)
        unregister(ws)

    await run_ws_connection(ws, cleanup=_cleanup)
```

- [ ] **Step 2: Verify + commit**

```bash
cd ~/Desktop/ivy-backend
python3 -c "import api.contact_book_ws" && echo "import OK"
git add api/contact_book_ws.py
git status --short
```

Expected: `M  api/contact_book_ws.py`。

```bash
git commit -m "$(cat <<'EOF'
feat(ws): parent_contact_book_ws 接 WS connection limiter

extract user_id (既有) + assert_under_limit 超則 close 1008；
register + cleanup unregister。

至此 4 個 WS endpoint (2 dismissal + 2 contact_book) 全接入 limiter；
per-user 跨 endpoint 共用 8 條上限。

Refs: docs/superpowers/specs/2026-05-28-ws-line-webhook-rate-limit-design.md §4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 全套 pytest 跑綠

**Files:** 無修改

- [ ] **Step 1: Run full pytest suite**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest --tb=no -q 2>&1 | tail -10
```

Expected: 既有 baseline ~5586 passed + 本 PR 新加 7 tests (6 limiter + 1 line webhook) ≈ **5593 passed**，無 new fail。

若有 fail：
- 若 fail 是 pre-existing flake（main 已有 5 個 tz fail 待 datetime-taipei 落地修），**不算 regression**
- 若是新引入的 fail 多半是：
  - 4 個 WS endpoint 既有 test fixture 與 limiter 不相容（既有 test 應該不會超 8 條，理論上不會炸）
  - import path 誤
  - line_webhook 既有 test 與 module-level limiter 互相干擾（low risk；既有 test 不會撞 1000 上限）
  各別 fix 後 re-run，全綠才進 Task 8。

- [ ] **Step 2: Diff 對比 origin/main**

```bash
cd ~/Desktop/ivy-backend
git diff --stat origin/main..HEAD
git log --oneline origin/main..HEAD
```

Expected: 約 7 個 commit（spec + plan + Task 1-6）/ 7 個檔案改動（1 helper + 1 helper test + 1 line_webhook + 1 line_webhook test + 2 ws endpoints + 1 spec + 1 plan）。

---

## Task 8: Push & Open PR

**Files:** 無修改；操作 GitHub 遠端

- [ ] **Step 1: Push branch**

```bash
cd ~/Desktop/ivy-backend
git push -u origin feat/ws-line-webhook-rate-limit-2026-05-28-backend 2>&1
```

Expected: branch pushed + tracking 設好。

- [ ] **Step 2: Create PR**

```bash
cd ~/Desktop/ivy-backend
gh pr create --title "feat(security): WS 連線上限 + LINE webhook rate-limit" --body "$(cat <<'EOF'
## Summary
- **\`utils/ws_connection_limiter.py\`** (new ~75 行)：per-user across endpoints 連線上限 8 條（in-memory，per-process）；assert_under_limit 超則 raise WSConnectionLimitExceeded → caller close(1008)
- **4 個 WS endpoint 接入**（dismissal portal + admin / contact_book portal + parent）：accept 前 assert + accept 後 register + cleanup unregister
- **\`api/line_webhook.py\`**：module-level limiter (1000 events / 5min per-channel) 用 create_limiter factory；signature verify 後、parse events 前 check
- 既有 LINE replay 三重防護（HMAC + ±5min skew + webhookEventId dedup）保留不動

## Behavior change
- 同 user_id 跨 4 個 WS endpoint 第 9 條連線拿 close(1008, "ws_connection_limit_exceeded")
- LINE webhook 同 channel 1001 events/5min 起拿 429
- Multi-instance：per-instance counter，total = instance × 8（WS 本來 sticky）

## Rollback
- WS limit 過嚴：\`WS_MAX_CONN_PER_USER = 9999\` 一行改 + restart
- LINE rate-limit 過嚴：註解 \`_LINE_WEBHOOK_LIMITER.check("channel")\` 一行 + restart

## Test plan
- [ ] CI 全綠（pytest ~5593 pass）
- [ ] 手動煙霧測 WS：開 8 個 tab 同帳號連 dismissal/contact_book → OK；第 9 個 → close(1008)
- [ ] 手動煙霧測 LINE webhook：用 curl + valid signature 連發 1001 次 → 第 1001 次 429
- [ ] Merge 後到 Sentry dashboard 設 alert rule：429 from /api/line/webhook (即時看 attack 嘗試)
- [ ] **Follow-up（不在本 PR）**：前端 close(1008) handler 應顯示「連線數上限」toast + stop reconnect loop

## Out of scope (follow-up per spec §9)
- 前端 close(1008) UX（toast + stop reconnect）
- WS_MAX_CONN_PER_USER env override
- Cross-instance WS counter
- LINE webhook per-source_user_id rate-limit

完整 spec：\`docs/superpowers/specs/2026-05-28-ws-line-webhook-rate-limit-design.md\`
完整 plan：\`docs/superpowers/plans/2026-05-28-ws-line-webhook-rate-limit.md\`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" 2>&1
```

Expected: PR URL printed。

- [ ] **Step 3: 等 CI 全綠（optional）**

```bash
gh pr checks <PR_NUM> --watch
```

或不等，跳到下個 sub-project。

---

## Self-Review

**1. Spec coverage:**
- §3 helper → Task 1 ✓
- §4 4 個 endpoint 接入 → Task 3 (portal dismissal) + Task 4 (admin dismissal) + Task 5 (portal contact_book) + Task 6 (parent contact_book) ✓
- §5 LINE webhook rate-limit → Task 2 ✓
- §6 測試 → Task 1 (6 helper tests) + Task 2 (1 line webhook integration) ✓; §6.3 endpoint integration test 已標 out-of-scope（plan 開頭 + spec §6.3 對齊）
- §7 行為變更 → PR body ✓
- §8 prereq → 無 user 操作 ✓
- §9 follow-up → PR body Out-of-scope 段 ✓
- §10 風險回退 → PR body Rollback ✓

**2. Placeholder scan:**
- Task 3 Step 2 「具體位置 Read 確認後決定」是 contingent guidance（既有檔 import 結構 mechanical lookup），非 placeholder
- Task 3 ⚠️ 注意「兩個 accept 都需...」是必要警示
- 所有 code block 完整
- 所有 commit message HEREDOC 完整

**3. Type consistency:**
- `WSConnectionLimitExceeded` exception class name Task 1 定義 ↔ Task 3-6 import + caught 一致 ✓
- `assert_under_limit(user_id)` signature Task 1 ↔ Task 3-6 call ✓
- `register(user_id, ws)` / `unregister(ws)` Task 1 ↔ Task 3-6 ✓
- `_LINE_WEBHOOK_LIMITER` module attribute name Task 2 定義 ↔ test monkeypatch ✓
- `"ws_connection_limit_exceeded"` close reason Task 1 spec ↔ Task 3-6 一致 ✓
- close code `1008` 一致 ✓
