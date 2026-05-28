# P1 韌性 Phase 3：Circuit breaker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox.

**Goal:** 純函式 in-process circuit breaker（CLOSED→OPEN→HALF_OPEN→CLOSED），3 個 module-level singleton（`LINE_BREAKER` / `SUPABASE_BREAKER` / `EXTERNAL_HTTP_BREAKER`），包入 18 個外呼站點防雪崩。**4xx 不算 trip**；timeout/5xx/network 連續 5 次觸發 OPEN 60s。OPEN 期間 LINE 走 Phase 2 retry 機制（caller 抓 `BreakerOpenError` → log row + line_next_retry_at）。

**Architecture:** ~80-line 純 Python（threading.Lock 確保 per-worker thread-safe；不上 Redis 分散式）。trip_on 由 caller 傳入過濾 exception type（避免 4xx 也 trip server breaker）。

**Spec**: `docs/superpowers/specs/2026-05-28-p1-external-integration-resilience-design.md` §4.2 + §7

---

## File Structure

| 動作 | 路徑 | 責任 |
|------|------|------|
| Create | `utils/circuit_breaker.py` | `CircuitBreaker` class + 3 singleton + `BreakerOpenError` |
| Create | `tests/test_circuit_breaker.py` | state machine unit test |
| Modify | `services/line_service.py` | `_record_line_response` 接 `BreakerOpenError` → 視為 server unavailable 上報；6 個 method 入口包 `LINE_BREAKER.call(...)` |
| Modify | `utils/supabase_storage.py` | 5 method 包 `SUPABASE_BREAKER.call(...)` |
| Modify | `services/recruitment_market_intelligence.py` | 3 helper 包 `EXTERNAL_HTTP_BREAKER.call(...)` |
| Modify | `services/geocoding_service.py` | 2 method 包 |
| Modify | `services/official_calendar.py` | 1 site 包 |
| Modify | `services/notification/dispatch.py` | LINE adapter exception 路徑接 `BreakerOpenError` (進入 Phase 2 retry path) |

---

## Task 1: `utils/circuit_breaker.py` + test (TDD)

**Files:**
- Create: `utils/circuit_breaker.py`
- Create: `tests/test_circuit_breaker.py`

- [ ] **Step 1.1: Write tests**

```python
# tests/test_circuit_breaker.py
"""Phase 3 P1 resilience：CircuitBreaker state machine + per-host singleton 行為."""
import time
import threading
import pytest


class TestCircuitBreakerStateMachine:
    def test_closed_calls_pass_through(self):
        from utils.circuit_breaker import CircuitBreaker
        b = CircuitBreaker("t", failure_threshold=3, recovery_seconds=1)
        assert b.call(lambda: "ok") == "ok"
        assert b.state == "closed"

    def test_failures_trip_to_open(self):
        from utils.circuit_breaker import CircuitBreaker, BreakerOpenError
        b = CircuitBreaker("t", failure_threshold=3, recovery_seconds=60)
        for _ in range(3):
            with pytest.raises(ConnectionError):
                b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        assert b.state == "open"

    def test_open_rejects_without_calling_fn(self):
        from utils.circuit_breaker import CircuitBreaker, BreakerOpenError
        b = CircuitBreaker("t", failure_threshold=1, recovery_seconds=60)
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        # state now open
        called = []
        with pytest.raises(BreakerOpenError):
            b.call(lambda: called.append(1) or "ok")
        assert called == []  # fn 未被呼叫

    def test_half_open_after_recovery(self):
        from utils.circuit_breaker import CircuitBreaker, BreakerOpenError
        b = CircuitBreaker("t", failure_threshold=1, recovery_seconds=0)  # immediate
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        time.sleep(0.01)
        # half_open 試 1 個成功 → close
        assert b.call(lambda: "ok") == "ok"
        assert b.state == "closed"

    def test_half_open_failure_reopens(self):
        from utils.circuit_breaker import CircuitBreaker, BreakerOpenError
        b = CircuitBreaker("t", failure_threshold=1, recovery_seconds=0)
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        time.sleep(0.01)
        # half_open 試一個失敗 → 再次 open
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("y")))
        assert b.state == "open"

    def test_trip_on_filter_4xx_not_tripped(self):
        """trip_on 不含 ValueError → 拋了不 trip，state 保 closed."""
        from utils.circuit_breaker import CircuitBreaker
        b = CircuitBreaker("t", failure_threshold=1, recovery_seconds=60, trip_on=(ConnectionError,))
        with pytest.raises(ValueError):
            b.call(lambda: (_ for _ in ()).throw(ValueError("client bug")))
        assert b.state == "closed"

    def test_success_resets_counter(self):
        from utils.circuit_breaker import CircuitBreaker
        b = CircuitBreaker("t", failure_threshold=3, recovery_seconds=60)
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        # success between → reset
        b.call(lambda: "ok")
        # 再連 2 次 fail 不應 trip（counter 已 reset）
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        with pytest.raises(ConnectionError):
            b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
        assert b.state == "closed"

    def test_stats_dict(self):
        from utils.circuit_breaker import CircuitBreaker
        b = CircuitBreaker("foo", failure_threshold=5, recovery_seconds=60)
        s = b.stats
        assert s["name"] == "foo"
        assert s["state"] == "closed"
        assert s["consecutive_failures"] == 0


class TestSingletons:
    def test_three_singletons_exist(self):
        from utils.circuit_breaker import LINE_BREAKER, SUPABASE_BREAKER, EXTERNAL_HTTP_BREAKER
        assert LINE_BREAKER.stats["name"] == "line"
        assert SUPABASE_BREAKER.stats["name"] == "supabase"
        assert EXTERNAL_HTTP_BREAKER.stats["name"] == "external_http"

    def test_singletons_independent(self):
        from utils.circuit_breaker import LINE_BREAKER, SUPABASE_BREAKER
        for _ in range(LINE_BREAKER._failure_threshold):
            try:
                LINE_BREAKER.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
            except ConnectionError:
                pass
        assert LINE_BREAKER.state == "open"
        assert SUPABASE_BREAKER.state == "closed"
        # cleanup for other tests
        LINE_BREAKER.reset()


class TestThreadSafety:
    def test_concurrent_failures(self):
        from utils.circuit_breaker import CircuitBreaker, BreakerOpenError
        b = CircuitBreaker("t", failure_threshold=10, recovery_seconds=60)

        def hit():
            for _ in range(5):
                try:
                    b.call(lambda: (_ for _ in ()).throw(ConnectionError("x")))
                except (ConnectionError, BreakerOpenError):
                    pass

        threads = [threading.Thread(target=hit) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        # 4 thread × 5 fail = 20 failure attempts；threshold=10 → state open
        assert b.state == "open"
```

- [ ] **Step 1.2: Implement `utils/circuit_breaker.py`**

```python
"""utils/circuit_breaker.py — 純 in-process circuit breaker (Phase 3 P1 resilience).

CLOSED → OPEN → HALF_OPEN → CLOSED state machine。Per-worker state，
不上 Redis 分散式（YAGNI；每 worker 獨立觀察獨立 trip 是設計選擇）。

對應 spec §4.2 + §7。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Literal, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
StateType = Literal["closed", "open", "half_open"]


class BreakerOpenError(Exception):
    """Caller 知道是 breaker 拒絕；不是真的失敗。Caller 自決後備行為。"""


class CircuitBreaker:
    """Simple in-process state machine.

    Args:
        name: identifier (for stats / logging)
        failure_threshold: 連續多少次失敗才 trip 到 OPEN
        recovery_seconds: OPEN 多久後進 HALF_OPEN 試探
        trip_on: 只有這些 type 的 exception 才算失敗；None = 全部算
    """

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_seconds: int = 60,
        trip_on: tuple[type[BaseException], ...] | None = None,
    ):
        self._name = name
        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._trip_on = trip_on
        self._lock = threading.Lock()
        self._state: StateType = "closed"
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> StateType:
        with self._lock:
            self._maybe_transition_to_half_open_locked()
            return self._state

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "name": self._name,
                "state": self._state,
                "consecutive_failures": self._consecutive_failures,
                "opened_at": self._opened_at,
                "failure_threshold": self._failure_threshold,
                "recovery_seconds": self._recovery_seconds,
            }

    def reset(self) -> None:
        """Test helper：清乾淨狀態。Prod code 不該呼叫。"""
        with self._lock:
            self._state = "closed"
            self._consecutive_failures = 0
            self._opened_at = None

    def call(self, fn: Callable[[], T]) -> T:
        """執行 fn；OPEN state 直接拋 BreakerOpenError。"""
        with self._lock:
            self._maybe_transition_to_half_open_locked()
            if self._state == "open":
                raise BreakerOpenError(
                    f"breaker '{self._name}' is open "
                    f"(consecutive_failures={self._consecutive_failures})"
                )

        try:
            result = fn()
        except BaseException as exc:
            # 是否算 trip 條件
            if self._trip_on is None or isinstance(exc, self._trip_on):
                with self._lock:
                    self._consecutive_failures += 1
                    if (
                        self._state == "half_open"
                        or self._consecutive_failures >= self._failure_threshold
                    ):
                        self._state = "open"
                        self._opened_at = time.time()
                        logger.warning(
                            "circuit breaker '%s' tripped to OPEN (failures=%s)",
                            self._name, self._consecutive_failures,
                        )
            raise

        # success
        with self._lock:
            self._consecutive_failures = 0
            if self._state == "half_open":
                self._state = "closed"
                self._opened_at = None
                logger.info("circuit breaker '%s' recovered to CLOSED", self._name)

        return result

    def _maybe_transition_to_half_open_locked(self) -> None:
        """Called under lock. OPEN + 時間到 → HALF_OPEN（接受 1 個試探）."""
        if self._state == "open" and self._opened_at is not None:
            if (time.time() - self._opened_at) >= self._recovery_seconds:
                self._state = "half_open"
                logger.info("circuit breaker '%s' entering HALF_OPEN", self._name)


# Module-level singletons
import requests as _requests  # only for default trip_on types

_HTTP_TRANSIENT_EXC = (
    _requests.exceptions.ConnectionError,
    _requests.exceptions.Timeout,
    _requests.exceptions.ChunkedEncodingError,
    ConnectionError,
    TimeoutError,
)

LINE_BREAKER = CircuitBreaker(
    "line", failure_threshold=5, recovery_seconds=60, trip_on=_HTTP_TRANSIENT_EXC,
)
SUPABASE_BREAKER = CircuitBreaker(
    "supabase", failure_threshold=5, recovery_seconds=60, trip_on=_HTTP_TRANSIENT_EXC,
)
EXTERNAL_HTTP_BREAKER = CircuitBreaker(
    "external_http", failure_threshold=10, recovery_seconds=120, trip_on=_HTTP_TRANSIENT_EXC,
)
```

- [ ] **Step 1.3: Verify tests pass**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/p1-resilience-phase1-be && pytest tests/test_circuit_breaker.py -v
```

All ~11 tests PASS expected.

- [ ] **Step 1.4: Commit**

```bash
git add utils/circuit_breaker.py tests/test_circuit_breaker.py
git commit -m "feat(resilience): utils/circuit_breaker — 3 singleton (Phase 3)

對應 spec §4.2。純 80 行 in-process state machine（CLOSED→OPEN→HALF_OPEN→CLOSED），
3 個 module-level singleton：LINE_BREAKER (threshold=5/60s) / SUPABASE_BREAKER
(5/60s) / EXTERNAL_HTTP_BREAKER (10/120s)。trip_on 只含 transient（4xx 不 trip
server breaker）。Per-worker state 是設計選擇（每 worker 獨立觀察獨立 trip）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 18 call site 包 breaker.call(...)

**Files**:
- Modify: `services/line_service.py` (6 method)
- Modify: `utils/supabase_storage.py` (5 method)
- Modify: `services/recruitment_market_intelligence.py` (3 helper)
- Modify: `services/geocoding_service.py` (2 method)
- Modify: `services/official_calendar.py` (1 site)
- Modify: `services/notification/dispatch.py` (LINE exception path 接 BreakerOpenError)

**核心 pattern**:

```python
# BEFORE
try:
    resp = requests.post(...)
    return _record_line_response(resp, context="_push_to_user")
except Exception as exc:
    return _record_line_response(exc, context="_push_to_user")

# AFTER
try:
    resp = LINE_BREAKER.call(lambda: requests.post(...))
    return _record_line_response(resp, context="_push_to_user")
except BreakerOpenError as exc:
    return _record_line_response(exc, context="_push_to_user(breaker_open)")
except Exception as exc:
    return _record_line_response(exc, context="_push_to_user")
```

`_record_line_response` 已存在 — `BreakerOpenError` 是 `Exception` 子類別會走 exception path 自動 `tagged_capture(level='error')`。caller 行為（return False）也對。**bonus**: dispatch._fan_out 看到 LINE adapter 拋 `BreakerOpenError`（其實也是 Exception）就走既有 Phase 2 retry 路徑寫 line_next_retry_at — 不需特別 wire。

- [ ] **Step 2.1: Add top imports + wrap line_service 6 method (用 bash python3)**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/p1-resilience-phase1-be && python3 <<'PY'
with open("services/line_service.py", "r") as f:
    content = f.read()

# Add import
old_imp = "from utils.external_calls import tagged_capture"
new_imp = "from utils.external_calls import tagged_capture\nfrom utils.circuit_breaker import LINE_BREAKER, BreakerOpenError"
content = content.replace(old_imp, new_imp, 1)

# Wrap each requests.post call inside breaker. Pattern：用 LINE_BREAKER.call(lambda: requests.post(...))
# 但 requests.post 結果可能是 success response，breaker 視為 success；exception 才 trip。
# 必須注意：HTTP 500 是 Response 物件 status_code=500，不 raise → 不會 trip breaker。這是 by design
# （HTTP 5xx 應透過 raise_for_status 或自訂 check；本 spec 不改 caller logic，breaker 只接 network/timeout）。

# 6 處 requests.post 都改成 LINE_BREAKER.call(lambda: requests.post(...))
# 用每個 method 的 _record_line_response context 字串當 unique anchor
import re

methods = [
    ("push_text_to_group", "_LINE_PUSH_URL,\n                headers={\"Authorization\": f\"Bearer {self._token}\"},\n                json={\n                    \"to\": group_id,\n                    \"messages\": [{\"type\": \"text\", \"text\": text}],\n                },\n                timeout=5,\n            )"),
]

# Simpler approach: 每個 method 內找 `resp = requests.post(...)` 整段，包成 LINE_BREAKER.call
# 用 _record_line_response 的 context 字串當 unique anchor 找該 method 範圍
contexts = [
    "push_text_to_group",
    "_push_to_user",
    "push_flex_to_user",
    "_push_to_user_with_quick_reply",
    "_reply_with_quick_reply",
    "_reply",
]

for ctx in contexts:
    # 找 `resp = requests.post(...)\n            return _record_line_response(resp, context="{ctx}")` pattern
    # 把 `resp = requests.post(...)` 改成 `resp = LINE_BREAKER.call(lambda: requests.post(...))`
    pat = re.compile(
        r'(\s+try:\n)(\s+resp = )requests\.post\((.*?)\)\n(\s+return _record_line_response\(resp, context="' + re.escape(ctx) + r'"\))',
        re.DOTALL,
    )
    def repl(m):
        indent_try, lhs, args, ret = m.group(1), m.group(2), m.group(3), m.group(4)
        return f'{indent_try}{lhs}LINE_BREAKER.call(lambda: requests.post({args}))\n{ret}'
    new_content, n = pat.subn(repl, content)
    if n != 1:
        raise RuntimeError(f"context={ctx}: expected 1 match, got {n}")
    content = new_content

with open("services/line_service.py", "w") as f:
    f.write(content)

import subprocess
n = subprocess.check_output(["grep", "-c", "LINE_BREAKER.call", "services/line_service.py"]).decode().strip()
print(f"LINE_BREAKER.call count: {n} (expected: 6)")
PY
```

- [ ] **Step 2.2: Wrap supabase_storage.py 5 method**

Similar pattern用 bash python3：

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/p1-resilience-phase1-be && python3 <<'PY'
with open("utils/supabase_storage.py", "r") as f:
    content = f.read()

old_imp = "from utils.external_calls import tagged_capture"
new_imp = "from utils.external_calls import tagged_capture\nfrom utils.circuit_breaker import SUPABASE_BREAKER, BreakerOpenError"
content = content.replace(old_imp, new_imp, 1)

# Wrap each SDK call inside try block with SUPABASE_BREAKER.call
# Pattern: bucket.{upload,download,remove,list,create_signed_url} → SUPABASE_BREAKER.call(lambda: bucket.X(...))
import re

# 5 個 method 的 wrap：每個 method 內 try 區塊內的 bucket.X(...) 包進 SUPABASE_BREAKER.call
# bucket.upload(...), bucket.download(...), bucket.remove([...]), bucket.list(...), bucket.create_signed_url(...)

# 簡化：手動逐 method 處理。先看每個 method 的 try 區塊
# save()
old_save = """        try:
            bucket.upload(
                path=key,
                file=data,
                file_options={\"content-type\": content_type, \"upsert\": \"true\"},
            )"""
new_save = """        try:
            SUPABASE_BREAKER.call(lambda: bucket.upload(
                path=key,
                file=data,
                file_options={\"content-type\": content_type, \"upsert\": \"true\"},
            ))"""
content = content.replace(old_save, new_save, 1)

# read()
old_read = """        try:
            return bucket.download(key)"""
new_read = """        try:
            return SUPABASE_BREAKER.call(lambda: bucket.download(key))"""
content = content.replace(old_read, new_read, 1)

# delete()
old_del = """        try:
            bucket.remove([key])"""
new_del = """        try:
            SUPABASE_BREAKER.call(lambda: bucket.remove([key]))"""
content = content.replace(old_del, new_del, 1)

# exists() — 注意 try 內有兩個 SDK 呼叫 (bucket.list)
# 這 method 比較複雜：parent.rsplit + bucket.list；只 wrap bucket.list 那兩處
# 先 inspect 原碼
print("--- exists() method ---")
import subprocess
print(subprocess.check_output(["sed", "-n", "/def exists/,/def public_url/p", "utils/supabase_storage.py"]).decode())

# signed_url()
old_signed = """        try:
            res = bucket.create_signed_url(key, ttl_seconds)"""
new_signed = """        try:
            res = SUPABASE_BREAKER.call(lambda: bucket.create_signed_url(key, ttl_seconds))"""
content = content.replace(old_signed, new_signed, 1)

with open("utils/supabase_storage.py", "w") as f:
    f.write(content)

n = subprocess.check_output(["grep", "-c", "SUPABASE_BREAKER.call", "utils/supabase_storage.py"]).decode().strip()
print(f"SUPABASE_BREAKER.call count: {n} (expected: 4-5; exists 視 implementation)")
PY
```

**exists() 處理**: 既有 try 區塊內呼叫 `bucket.list()` 兩次（含 if/else branch）。同樣用 lambda 包，每處：
```python
items = SUPABASE_BREAKER.call(lambda: bucket.list())
items = SUPABASE_BREAKER.call(lambda b=parent[0]: bucket.list(b))
```
或更簡單 wrap 整個 try block 邏輯成 inner function。selectively decide based on actual code.

- [ ] **Step 2.3: Wrap external HTTP 3 service**

`services/recruitment_market_intelligence.py` 3 helper：每個 `_request_json` / `_request_text` / `_post_json` 內的 `requests.get/post` 包 `EXTERNAL_HTTP_BREAKER.call(lambda: requests.get(...))`。

`services/geocoding_service.py` 2 helper：同 pattern。

`services/official_calendar.py` 1 site：同 pattern。

每個 file top import：
```python
from utils.circuit_breaker import EXTERNAL_HTTP_BREAKER, BreakerOpenError
```

`BreakerOpenError` 屬於 `Exception` 子類別，既有 try/except Exception 自動接到，會走 `tagged_capture(exc, tag='external_http')` + raise。caller scheduler 行為不變。

- [ ] **Step 2.4: dispatch.py LINE adapter 路徑（已自動 work）**

`LINE_BREAKER` OPEN 時 LINE adapter `send()` 內部 `LINE_BREAKER.call(...)` 拋 `BreakerOpenError`（屬 Exception）。`dispatch._fan_out` 既有 `except Exception` 路徑（Phase 2 加的 schedule retry 邏輯）會接到並寫 `line_next_retry_at` — 不需特別改 dispatch.py。

但**確認一下**：LINE adapter `send()` 內部 handler 是否 try/except 吞掉了 exception？

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/p1-resilience-phase1-be && grep -n 'except\|try' services/notification/_channels/line.py | head
```

若 LINE_HANDLERS 內 try/except 吞掉 BreakerOpenError，需在 adapter `send()` 層加 re-raise；或在 handler 內加 `except BreakerOpenError: raise` allow-list。

- [ ] **Step 2.5: Run focused tests**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/p1-resilience-phase1-be && pytest tests/test_circuit_breaker.py tests/test_line_service.py tests/test_supabase_storage.py tests/test_external_http_tagged_capture.py tests/notification/ -v 2>&1 | tail -30
```

Expected: 全綠 + 0 regression。如 line_service 既有 mock 直接 `monkeypatch.setattr("services.line_service.requests.post", ...)` 仍 work（因 lambda 在 method 內定義，requests.post 仍是 monkeypatch 後的 ref）。

- [ ] **Step 2.6: Commit**

```bash
git add services/line_service.py utils/supabase_storage.py services/recruitment_market_intelligence.py services/geocoding_service.py services/official_calendar.py services/notification/_channels/line.py
git commit -m "feat(resilience): 18 external call sites wrap breaker (Phase 3)

對應 spec §7。
- LINE 6 method: LINE_BREAKER.call(lambda: requests.post(...))
- Supabase 5 method: SUPABASE_BREAKER.call(lambda: bucket.X(...))
- 3 external HTTP service 6 site: EXTERNAL_HTTP_BREAKER.call(...)

BreakerOpenError 屬 Exception 子類別 → 既有 _record_line_response /
tagged_capture / dispatch retry path 自動接（無需 special wiring）。
LINE OPEN 時 dispatch._fan_out 接 exception 走 Phase 2 retry。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## 完成定義

Phase 3 ship = 2 atomic commit：
1. utils/circuit_breaker.py + 11 unit test
2. 18 call site wrap + 既有 test 零 regression
