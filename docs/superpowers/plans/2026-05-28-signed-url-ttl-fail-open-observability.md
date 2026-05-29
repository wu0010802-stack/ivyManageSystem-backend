# Signed URL TTL + Fail-open Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backend signed URL TTL 從 3600s 縮短到 300s + 7 個 fail-open 點接 `capture_fail_open` helper 送 Sentry tag + capture_exception。

**Architecture:** 新 `utils/fail_open.py` 提供 `capture_fail_open(operation, error, **extra)` 共用 helper（log warning + Sentry push_scope + set_tag + capture_exception）；7 個 fail-open call site surgical 替換既有 `logger.warning(...)` 一行，fail-open 行為 100% 等同。Signed URL TTL 只改 `config/storage.py` default，3 個 call site 透過 `settings.storage.supabase_signed_url_ttl` 自動生效。

**Tech Stack:** Python 3.9 / FastAPI / SQLAlchemy / pytest / sentry-sdk[fastapi]

**Spec:** `docs/superpowers/specs/2026-05-28-signed-url-ttl-fail-open-observability-design.md`

**重要 context：**
- 當前 git branch 應為 `feat/signed-url-ttl-fail-open-2026-05-28-backend`（spec commit `59607b5` 已在上）。執行前確認：`git branch --show-current`
- 若不在該 branch：`cd ~/Desktop/ivy-backend && git checkout feat/signed-url-ttl-fail-open-2026-05-28-backend`
- Backend working tree 可能有 user 並行 WIP（`pyproject.toml` 等）。**不 stash、不 `git add -A`**，所有 commit 只 add 本 plan 列具體檔案。

---

## File Structure

```
ivy-backend/
├── config/storage.py                    ← Task 2 (Modify line 23: 3600→300)
├── utils/
│   ├── fail_open.py                     ← Task 1 (Create ~30 行)
│   ├── auth.py                          ← Task 3 (Modify line 240 + add import)
│   ├── rate_limit.py                    ← Task 4 (Modify 2 處 + add import)
│   └── rate_limit_db.py                 ← Task 5 (Modify 4 處 + add import)
└── tests/
    ├── test_fail_open.py                ← Task 1 (Create 4 tests)
    ├── test_config/test_storage.py      ← Task 2 (Modify line 22)
    ├── test_jwt_blocklist.py            ← Task 3 (Add 1 regression test)
    ├── test_rate_limit_pg.py            ← Task 4 (Add 2 regression tests)
    └── test_rate_limit_db.py            ← Task 5 (Add 4 regression tests)
```

---

## Task 1: `capture_fail_open` Helper (TDD)

**Files:**
- Create: `ivy-backend/utils/fail_open.py`
- Test: `ivy-backend/tests/test_fail_open.py`

- [ ] **Step 1: Confirm on correct branch**

```bash
cd ~/Desktop/ivy-backend
git branch --show-current
```

Expected: `feat/signed-url-ttl-fail-open-2026-05-28-backend`

- [ ] **Step 2: Write the failing test file**

Create `~/Desktop/ivy-backend/tests/test_fail_open.py`：

```python
"""capture_fail_open helper 單元測試。"""
from unittest.mock import MagicMock, patch

from utils.fail_open import capture_fail_open


def test_capture_fail_open_logs_warning(caplog):
    """應 log warning 含 operation 名稱與 error message。"""
    err = RuntimeError("DB down")
    with caplog.at_level("WARNING"):
        capture_fail_open("is_token_revoked", err)
    assert "is_token_revoked" in caplog.text
    assert "DB down" in caplog.text


def test_capture_fail_open_sets_sentry_tag_and_captures():
    """應 push_scope + set_tag('fail_open', operation) + capture_exception。"""
    fake_scope = MagicMock()
    fake_scope_cm = MagicMock()
    fake_scope_cm.__enter__ = MagicMock(return_value=fake_scope)
    fake_scope_cm.__exit__ = MagicMock(return_value=False)

    with patch(
        "utils.fail_open.sentry_sdk.push_scope", return_value=fake_scope_cm
    ) as mock_push, patch(
        "utils.fail_open.sentry_sdk.capture_exception"
    ) as mock_capture:
        err = RuntimeError("DB down")
        capture_fail_open("is_token_revoked", err)
        mock_push.assert_called_once()
        fake_scope.set_tag.assert_any_call("fail_open", "is_token_revoked")
        mock_capture.assert_called_once_with(err)


def test_capture_fail_open_extra_tags_prefixed_and_stringified():
    """extra kwargs 應以 fail_open.{key} 設 tag + str() 處理 value。"""
    fake_scope = MagicMock()
    fake_scope_cm = MagicMock()
    fake_scope_cm.__enter__ = MagicMock(return_value=fake_scope)
    fake_scope_cm.__exit__ = MagicMock(return_value=False)

    with patch(
        "utils.fail_open.sentry_sdk.push_scope", return_value=fake_scope_cm
    ), patch("utils.fail_open.sentry_sdk.capture_exception"):
        capture_fail_open(
            "rate_limit.check",
            RuntimeError("x"),
            name="login",
            key="ip:1.2.3.4",
            count=42,
        )
        fake_scope.set_tag.assert_any_call("fail_open.name", "login")
        fake_scope.set_tag.assert_any_call("fail_open.key", "ip:1.2.3.4")
        fake_scope.set_tag.assert_any_call("fail_open.count", "42")


def test_capture_fail_open_no_extra_works():
    """無 extra kwargs 也應正常 capture。"""
    fake_scope = MagicMock()
    fake_scope_cm = MagicMock()
    fake_scope_cm.__enter__ = MagicMock(return_value=fake_scope)
    fake_scope_cm.__exit__ = MagicMock(return_value=False)

    with patch(
        "utils.fail_open.sentry_sdk.push_scope", return_value=fake_scope_cm
    ), patch("utils.fail_open.sentry_sdk.capture_exception") as mock_capture:
        capture_fail_open("op", RuntimeError("x"))
        mock_capture.assert_called_once()
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_fail_open.py -v 2>&1 | tail -15
```

Expected: 4 tests fail with `ModuleNotFoundError: No module named 'utils.fail_open'`

- [ ] **Step 4: Create the helper**

Create `~/Desktop/ivy-backend/utils/fail_open.py`：

```python
"""Fail-open 共用 observability helper。

把散落在 utils/auth.py、utils/rate_limit.py、utils/rate_limit_db.py
等處的「logger.warning + return False/None」fail-open 集中成同一 helper：
保留 fail-open 行為（不擋請求避免 DB 抖動全站 down），但加 Sentry tag
+ capture_exception 讓 ops 在 DB 大範圍失聯時看得到。
"""
import logging
from typing import Any

import sentry_sdk

logger = logging.getLogger(__name__)


def capture_fail_open(operation: str, error: Exception, **extra: Any) -> None:
    """記錄 fail-open 事件並送 Sentry。

    Args:
        operation: fail-open 點識別字串，格式 `{module}.{function}`（如
            "is_token_revoked"、"rate_limit_db.bump_failed_login"）。
            穩定字串用於 Sentry dashboard filter/alert rule。
        error: 觸發 fail-open 的 exception
        **extra: 額外 tag context（如 key、jti、name），以
            `fail_open.{key}` 形式設成 Sentry tag。限 primitive value
            (str/int/bool)；dict/list 等複合型別請呼叫端先序列化。
    """
    logger.warning("%s 失敗，fail-open: %s", operation, error)
    with sentry_sdk.push_scope() as scope:
        scope.set_tag("fail_open", operation)
        for k, v in extra.items():
            scope.set_tag(f"fail_open.{k}", str(v))
        sentry_sdk.capture_exception(error)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_fail_open.py -v 2>&1 | tail -15
```

Expected: 4 passed in <2s

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add utils/fail_open.py tests/test_fail_open.py
git status --short
```

Expected: 2 行 `A  utils/fail_open.py` + `A  tests/test_fail_open.py`；user WIP 其他檔仍 unstaged。

```bash
git commit -m "$(cat <<'EOF'
feat(utils): 加入 capture_fail_open helper

Fail-open 點共用 observability：log warning + Sentry
push_scope.set_tag('fail_open', operation) + capture_exception。
保留 fail-open 行為（不擋請求），加 Sentry tag 讓 ops 在 DB
大範圍失聯時看得到。

Refs: docs/superpowers/specs/2026-05-28-signed-url-ttl-fail-open-observability-design.md §4

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Signed URL TTL Config 收斂

**Files:**
- Modify: `ivy-backend/config/storage.py:23` (1 行)
- Test: `ivy-backend/tests/test_config/test_storage.py:22` (1 行)

- [ ] **Step 1: Update test assertion first (TDD)**

Use Edit tool on `~/Desktop/ivy-backend/tests/test_config/test_storage.py`：

- old: `    assert s.supabase_signed_url_ttl == 3600`
- new: `    assert s.supabase_signed_url_ttl == 300`

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_config/test_storage.py -v 2>&1 | tail -15
```

Expected: 對應 default test fail（actual 3600 != expected 300）；其他 test pass。

- [ ] **Step 3: Update config default**

Use Edit tool on `~/Desktop/ivy-backend/config/storage.py`：

- old:
```
    supabase_signed_url_ttl: int = Field(
        default=3600, validation_alias="SUPABASE_STORAGE_SIGNED_URL_TTL"
    )
```
- new:
```
    supabase_signed_url_ttl: int = Field(
        default=300,  # 3600→300（5 分鐘）。Prod 若 UX 有問題，env override 可暫 revert
        validation_alias="SUPABASE_STORAGE_SIGNED_URL_TTL",
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_config/test_storage.py -v 2>&1 | tail -15
```

Expected: all pass。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add config/storage.py tests/test_config/test_storage.py
git status --short
```

Expected: 2 行 `M  config/storage.py` + `M  tests/test_config/test_storage.py`。

```bash
git commit -m "$(cat <<'EOF'
chore(config): signed URL TTL default 3600s → 300s

3 call site (api/leaves.py + api/portal/leaves.py + api/portfolio/reports.py)
都讀 settings.storage.supabase_signed_url_ttl，改 config default 自動生效。

Prod 若 UX 有問題：env SUPABASE_STORAGE_SIGNED_URL_TTL=3600 即時 revert。

Refs: docs/superpowers/specs/2026-05-28-signed-url-ttl-fail-open-observability-design.md §3

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `is_token_revoked` Fail-open Instrumentation

**Files:**
- Modify: `ivy-backend/utils/auth.py:240` (1 處)
- Test: `ivy-backend/tests/test_jwt_blocklist.py` (add 1 test)

- [ ] **Step 1: Write the failing regression test**

Append to `~/Desktop/ivy-backend/tests/test_jwt_blocklist.py`：

```python
def test_is_token_revoked_db_failure_calls_capture_fail_open(monkeypatch):
    """DB raise 時應 capture_fail_open + 仍回 False（fail-open 行為保留）。"""
    calls = []

    def fake_capture(operation, error, **extra):
        calls.append((operation, type(error).__name__, extra))

    monkeypatch.setattr("utils.auth.capture_fail_open", fake_capture)

    class BrokenEngine:
        def connect(self):
            raise RuntimeError("DB down")

    monkeypatch.setattr("utils.auth.get_engine", lambda: BrokenEngine())

    from utils.auth import is_token_revoked
    assert is_token_revoked("any-jti") is False  # fail-open 行為
    assert len(calls) == 1
    assert calls[0][0] == "is_token_revoked"
    assert calls[0][1] == "RuntimeError"
    assert calls[0][2] == {"jti": "any-jti"}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_jwt_blocklist.py::test_is_token_revoked_db_failure_calls_capture_fail_open -v 2>&1 | tail -10
```

Expected: FAIL — `monkeypatch.setattr("utils.auth.capture_fail_open", ...)` raises AttributeError (capture_fail_open 尚未 import 進 utils/auth.py)。

- [ ] **Step 3: Update utils/auth.py — add import + replace fail-open**

讀 `~/Desktop/ivy-backend/utils/auth.py` 確認既有 import 區（line 1-30），決定 import 加在哪。預期既有有：
```python
import logging
...
logger = logging.getLogger(__name__)
```

Use Edit tool 加 import（找一個既有 utils import 後加）：

- 找既有 line 範例 `from utils.errors import ...` 或類似 utils import；若無，在 standard library import 區後加：
- 加入新行：
```python
from utils.fail_open import capture_fail_open
```

（若 utils/auth.py 沒既有 `from utils....` import，加在最後一個 stdlib import 後即可。記得 import 段保持 sorted alphabetically — utils 排在 stdlib 之後。）

接著 Use Edit tool 替換 fail-open block：

- old:
```python
    except Exception as e:
        logger.warning("is_token_revoked 查詢失敗，fail-open: %s", e)
        return False
```
- new:
```python
    except Exception as e:
        capture_fail_open("is_token_revoked", e, jti=jti)
        return False
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_jwt_blocklist.py -v 2>&1 | tail -10
```

Expected: 新 test pass + 既有 jwt_blocklist test 全 pass（fail-open 行為 100% 等同）。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add utils/auth.py tests/test_jwt_blocklist.py
git status --short
```

Expected: `M  utils/auth.py` + `M  tests/test_jwt_blocklist.py`。

```bash
git commit -m "$(cat <<'EOF'
feat(auth): is_token_revoked fail-open 接 capture_fail_open

DB 失敗時 log warning + Sentry tag('fail_open', 'is_token_revoked')
+ capture_exception；行為仍 fail-open（return False）。

DB 大範圍失聯時 ops 可在 Sentry dashboard 立即看到 + alert。

Refs: docs/superpowers/specs/2026-05-28-signed-url-ttl-fail-open-observability-design.md §5.1 #1

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `utils/rate_limit.py` 2 處 Fail-open Instrumentation

**Files:**
- Modify: `ivy-backend/utils/rate_limit.py` (2 處 + import)
- Test: `ivy-backend/tests/test_rate_limit_pg.py` (add 2 tests)

- [ ] **Step 1: Write failing regression tests**

Read `~/Desktop/ivy-backend/tests/test_rate_limit_pg.py` 確認既有 fixture `db` 結構，append 兩個新 test 到檔尾：

```python
def test_postgres_limiter_db_failure_calls_capture_fail_open(monkeypatch):
    """PostgresLimiter.check DB raise 時應 capture_fail_open + 不擋（fail-open）。"""
    calls = []

    def fake_capture(operation, error, **extra):
        calls.append((operation, type(error).__name__, extra))

    monkeypatch.setattr("utils.rate_limit.capture_fail_open", fake_capture)

    class BrokenEngine:
        def begin(self):
            raise RuntimeError("DB down")

    monkeypatch.setattr("utils.rate_limit.get_engine", lambda: BrokenEngine())

    from utils.rate_limit import PostgresLimiter
    limiter = PostgresLimiter(max_calls=5, window_seconds=60, name="test")
    # fail-open: 不拋 HTTPException(429) 即視為 pass
    limiter.check("ip:1.2.3.4")

    assert len(calls) == 1
    assert calls[0][0] == "rate_limit.postgres_limiter"
    assert calls[0][1] == "RuntimeError"
    assert calls[0][2] == {"name": "test", "key": "ip:1.2.3.4"}


def test_cleanup_rate_limit_buckets_db_failure_calls_capture_fail_open(monkeypatch):
    """cleanup_rate_limit_buckets DB raise 時應 capture_fail_open + 回 0。"""
    calls = []

    def fake_capture(operation, error, **extra):
        calls.append((operation, type(error).__name__, extra))

    monkeypatch.setattr("utils.rate_limit.capture_fail_open", fake_capture)

    class BrokenEngine:
        def begin(self):
            raise RuntimeError("DB down")

    monkeypatch.setattr("utils.rate_limit.get_engine", lambda: BrokenEngine())

    from utils.rate_limit import cleanup_rate_limit_buckets
    result = cleanup_rate_limit_buckets(retention_minutes=5)
    assert result == 0  # fail-open: 回 0 不拋

    assert len(calls) == 1
    assert calls[0][0] == "rate_limit.cleanup_buckets"
    assert calls[0][1] == "RuntimeError"
    assert calls[0][2] == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_rate_limit_pg.py::test_postgres_limiter_db_failure_calls_capture_fail_open tests/test_rate_limit_pg.py::test_cleanup_rate_limit_buckets_db_failure_calls_capture_fail_open -v 2>&1 | tail -15
```

Expected: 2 fail — `monkeypatch.setattr("utils.rate_limit.capture_fail_open", ...)` AttributeError。

- [ ] **Step 3: Update utils/rate_limit.py**

Use Edit tool 加 import（在既有 `from utils.request_ip import get_client_ip` 後）：
```python
from utils.fail_open import capture_fail_open
```

Use Edit tool 替換 #1 fail-open (line ~145，`PostgresLimiter.check`)：

- old:
```python
        except Exception as e:
            logger.warning(
                "PostgresLimiter [%s] DB 操作失敗，fail-open: %s",
                self.name,
                e,
            )
```
- new:
```python
        except Exception as e:
            capture_fail_open(
                "rate_limit.postgres_limiter", e, name=self.name, key=key
            )
```

Use Edit tool 替換 #2 fail-open (line ~212，`cleanup_rate_limit_buckets`)：

- old:
```python
    except Exception as e:
        logger.warning("cleanup_rate_limit_buckets 失敗: %s", e)
        return 0
```
- new:
```python
    except Exception as e:
        capture_fail_open("rate_limit.cleanup_buckets", e)
        return 0
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_rate_limit_pg.py -v 2>&1 | tail -20
```

Expected: 2 new test pass + 既有 rate_limit_pg test 全 pass。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add utils/rate_limit.py tests/test_rate_limit_pg.py
git status --short
```

Expected: `M  utils/rate_limit.py` + `M  tests/test_rate_limit_pg.py`。

```bash
git commit -m "$(cat <<'EOF'
feat(rate_limit): 2 處 fail-open 接 capture_fail_open

- PostgresLimiter.check DB 失敗 → fail_open=rate_limit.postgres_limiter
- cleanup_rate_limit_buckets DB 失敗 → fail_open=rate_limit.cleanup_buckets

行為仍 fail-open（不擋請求 / 回 0），加 Sentry tag + capture_exception。

Refs: docs/superpowers/specs/2026-05-28-signed-url-ttl-fail-open-observability-design.md §5.1 #2 #3

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `utils/rate_limit_db.py` 4 處 Fail-open Instrumentation

**Files:**
- Modify: `ivy-backend/utils/rate_limit_db.py` (4 處 + import)
- Test: `ivy-backend/tests/test_rate_limit_db.py` (add 4 tests)

- [ ] **Step 1: Read existing utils/rate_limit_db.py 結構**

讀 `~/Desktop/ivy-backend/utils/rate_limit_db.py` 完整檔，確認 4 個 fail-open 點：
- line ~99: `bump_failed_login`
- line ~134: `bump_login_streak`
- line ~157: `clear_login_failures`
- line ~190: `earliest_attempt_at`

各 function 簽章記下（param name 將決定 test mock 與 helper extra kwargs）。

- [ ] **Step 2: Write failing regression tests**

Append 到 `~/Desktop/ivy-backend/tests/test_rate_limit_db.py` 檔尾。
**注意**：以下 test code 假設既有 fixture `db_engine`；若實際 fixture 名不同，調整 monkeypatch target 或改用 patch get_engine。

```python
def test_bump_failed_login_db_failure_calls_capture_fail_open(monkeypatch):
    """bump_failed_login DB raise 時應 capture_fail_open + 不拋。"""
    calls = []

    def fake_capture(operation, error, **extra):
        calls.append((operation, type(error).__name__, extra))

    monkeypatch.setattr("utils.rate_limit_db.capture_fail_open", fake_capture)

    class BrokenEngine:
        def begin(self):
            raise RuntimeError("DB down")

    monkeypatch.setattr("utils.rate_limit_db.get_engine", lambda: BrokenEngine())

    from utils.rate_limit_db import bump_failed_login
    bump_failed_login("ip:1.2.3.4")  # fail-open: 不拋

    assert len(calls) == 1
    assert calls[0][0] == "rate_limit_db.bump_failed_login"
    assert calls[0][1] == "RuntimeError"
    assert calls[0][2] == {"key": "ip:1.2.3.4"}


def test_bump_login_streak_db_failure_calls_capture_fail_open(monkeypatch):
    """bump_login_streak DB raise 時應 capture_fail_open + 不拋。"""
    calls = []

    def fake_capture(operation, error, **extra):
        calls.append((operation, type(error).__name__, extra))

    monkeypatch.setattr("utils.rate_limit_db.capture_fail_open", fake_capture)

    class BrokenEngine:
        def begin(self):
            raise RuntimeError("DB down")

    monkeypatch.setattr("utils.rate_limit_db.get_engine", lambda: BrokenEngine())

    from utils.rate_limit_db import bump_login_streak
    bump_login_streak("ip:1.2.3.4")

    assert len(calls) == 1
    assert calls[0][0] == "rate_limit_db.bump_login_streak"
    assert calls[0][1] == "RuntimeError"
    assert calls[0][2] == {"key": "ip:1.2.3.4"}


def test_clear_login_failures_db_failure_calls_capture_fail_open(monkeypatch):
    """clear_login_failures DB raise 時應 capture_fail_open + 不拋。"""
    calls = []

    def fake_capture(operation, error, **extra):
        calls.append((operation, type(error).__name__, extra))

    monkeypatch.setattr("utils.rate_limit_db.capture_fail_open", fake_capture)

    class BrokenEngine:
        def begin(self):
            raise RuntimeError("DB down")

    monkeypatch.setattr("utils.rate_limit_db.get_engine", lambda: BrokenEngine())

    from utils.rate_limit_db import clear_login_failures
    clear_login_failures("ip:1.2.3.4")

    assert len(calls) == 1
    assert calls[0][0] == "rate_limit_db.clear_login_failures"
    assert calls[0][1] == "RuntimeError"
    assert calls[0][2] == {"key": "ip:1.2.3.4"}


def test_earliest_attempt_at_db_failure_calls_capture_fail_open(monkeypatch):
    """earliest_attempt_at DB raise 時應 capture_fail_open + 回 None。"""
    calls = []

    def fake_capture(operation, error, **extra):
        calls.append((operation, type(error).__name__, extra))

    monkeypatch.setattr("utils.rate_limit_db.capture_fail_open", fake_capture)

    class BrokenEngine:
        def begin(self):
            raise RuntimeError("DB down")

    monkeypatch.setattr("utils.rate_limit_db.get_engine", lambda: BrokenEngine())

    from utils.rate_limit_db import earliest_attempt_at
    result = earliest_attempt_at("login", "ip:1.2.3.4")
    assert result is None  # fail-open: 回 None

    assert len(calls) == 1
    assert calls[0][0] == "rate_limit_db.earliest_attempt_at"
    assert calls[0][1] == "RuntimeError"
    assert calls[0][2] == {"scope": "login", "key": "ip:1.2.3.4"}
```

⚠️ **若實際 function 簽章不同**（例如 `bump_failed_login(scope, key)` 而非 `bump_failed_login(key)`），先用 Read 工具看真實簽章後調整 test 與 expected `extra` dict。

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_rate_limit_db.py -k capture_fail_open -v 2>&1 | tail -20
```

Expected: 4 fail — capture_fail_open AttributeError。

- [ ] **Step 4: Update utils/rate_limit_db.py**

Use Edit tool 加 import：
```python
from utils.fail_open import capture_fail_open
```

Use Edit tool 替換 4 處 fail-open（每處 except 改用 helper；extra kwargs 對應實際 function 簽章）：

**#4 `bump_failed_login` line ~99：**
- old: `logger.warning(...)` 該行（依實際讀到內容）
- new:
```python
        capture_fail_open(
            "rate_limit_db.bump_failed_login", e, key=key
        )
```
（若簽章是 `(scope, key)`，extra 改 `scope=scope, key=key`）

**#5 `bump_login_streak` line ~134：**
- old: `logger.warning(...)` 該行
- new:
```python
        capture_fail_open(
            "rate_limit_db.bump_login_streak", e, key=key
        )
```

**#6 `clear_login_failures` line ~157：**
- old: `logger.warning(...)` 該行
- new:
```python
        capture_fail_open(
            "rate_limit_db.clear_login_failures", e, key=key
        )
```

**#7 `earliest_attempt_at` line ~190：**
- old:
```python
    except Exception as e:
        logger.warning("earliest_attempt_at 失敗 [%s:%s]: %s", scope, key, e)
        return None
```
- new:
```python
    except Exception as e:
        capture_fail_open(
            "rate_limit_db.earliest_attempt_at", e, scope=scope, key=key
        )
        return None
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest tests/test_rate_limit_db.py -v 2>&1 | tail -20
```

Expected: 4 new test pass + 既有 rate_limit_db test 全 pass。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add utils/rate_limit_db.py tests/test_rate_limit_db.py
git status --short
```

Expected: `M  utils/rate_limit_db.py` + `M  tests/test_rate_limit_db.py`。

```bash
git commit -m "$(cat <<'EOF'
feat(rate_limit_db): 4 處 fail-open 接 capture_fail_open

- bump_failed_login / bump_login_streak / clear_login_failures /
  earliest_attempt_at DB 失敗 → fail_open=rate_limit_db.{function}

行為仍 fail-open（不擋登入 / 回 0 / 回 None），加 Sentry tag +
capture_exception 讓 DB 大範圍失聯時 ops 立即看到。

Refs: docs/superpowers/specs/2026-05-28-signed-url-ttl-fail-open-observability-design.md §5.1 #4 #5 #6 #7

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 全套 pytest 跑綠

**Files:** 無修改；最後 regression check

- [ ] **Step 1: Run full pytest suite**

```bash
cd ~/Desktop/ivy-backend
python3 -m pytest 2>&1 | tail -30
```

Expected: 既有 baseline `5103 passed` + 本 PR 新加 ~11 tests (4 fail_open + 1 jwt_blocklist + 2 rate_limit_pg + 4 rate_limit_db) ≈ **5114 passed**，無 new fail。

若有 fail：
- 若 fail 是 pre-existing flake（MEMORY 提到 main 有 5 個 tz fail 待 datetime-taipei 落地修），**不算 regression**
- 若是新引入的 fail：必為以下之一：
  - 簽章不符（Task 5 step 4 注意事項）
  - import 路徑錯（`utils.fail_open.capture_fail_open` vs `utils.module.capture_fail_open` mock 點）
  - regression test fixture 名稱不符
  各別 fix 後 re-run，全綠才進 Task 7。

- [ ] **Step 2: Diff 對比 origin/main 確認改動範圍**

```bash
cd ~/Desktop/ivy-backend
git diff --stat origin/main..HEAD
```

Expected: 約 9 個檔案改動（spec + plan + helper + tests + 4 source files）。**沒有意料外的檔案**。

```bash
git log --oneline origin/main..HEAD
```

Expected: 6 commit (spec + plan + Task 1-5 各 1 commit)。

---

## Task 7: Push Branch & Open PR

**Files:** 無修改；操作 GitHub 遠端

- [ ] **Step 1: Push branch**

```bash
cd ~/Desktop/ivy-backend
git push -u origin feat/signed-url-ttl-fail-open-2026-05-28-backend 2>&1
```

Expected: branch pushed + tracking 設好。

- [ ] **Step 2: Create PR**

```bash
cd ~/Desktop/ivy-backend
gh pr create --title "feat: signed URL TTL 收斂 + fail-open Sentry observability" --body "$(cat <<'EOF'
## Summary
- **`config/storage.py`**：signed URL TTL default 3600s → 300s（leave 附件 + growth report PDF）；env `SUPABASE_STORAGE_SIGNED_URL_TTL` 可 override 暫 revert
- **`utils/fail_open.py`** (new)：`capture_fail_open(operation, error, **extra)` helper 共用 fail-open observability
- **7 個 fail-open 點接 helper**：
  - `utils/auth.py:is_token_revoked`
  - `utils/rate_limit.py:PostgresLimiter.check` + `cleanup_rate_limit_buckets`
  - `utils/rate_limit_db.py:bump_failed_login` + `bump_login_streak` + `clear_login_failures` + `earliest_attempt_at`
- **既有 fail-open 行為 100% 保留**（return False / 0 / None / 不拋 HTTPException），只加 Sentry tag + capture_exception 讓 ops 在 DB 大範圍失聯時看得到
- 新 11 tests（4 helper + 7 regression 各 fail-open 點 1 條）

## Behavior change
- HR/家長端點請假附件 / 成長報告連結，超過 5 min 後 URL expired（自動 re-fetch）— UX 影響低
- Sentry 預估月 +50-200 events（fail-open 是異常情況，正常零）；若 prod 噴量爆代表真有 incident
- Prod rollback：env `SUPABASE_STORAGE_SIGNED_URL_TTL=3600` 即時 revert

## Test plan
- [ ] CI 全綠（pytest 5114+ pass）
- [ ] Merge 後到 Sentry dashboard 設 alert rule：`fail_open:is_token_revoked count() > 5 in 5min`
- [ ] 手動煙霧測：上傳 leave 附件 → 5 min 後重 fetch → 應觸發新 URL（不報 403）

完整 spec：`docs/superpowers/specs/2026-05-28-signed-url-ttl-fail-open-observability-design.md`
完整 plan：`docs/superpowers/plans/2026-05-28-signed-url-ttl-fail-open-observability.md`

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
- §3 Signed URL TTL → Task 2 ✓
- §4 helper → Task 1 ✓
- §5 7 個 call site → Task 3 (1) + Task 4 (2) + Task 5 (4) ✓
- §6 測試 → Task 1 (helper) + Task 2 (config) + Task 3-5 (regression) ✓
- §7-§9 行為變更/prereq/follow-up → PR body 提及 ✓
- §10 風險回退 → PR body 含 rollback ✓

**2. Placeholder scan:**
- Task 5 step 2 有 ⚠️「若實際簽章不同」備註，但已給 implementer 具體 fallback path（讀 source 後調整）— 屬 contingent guidance 非 placeholder
- 所有 Edit pattern 的 old/new 完整提供
- 所有 git command 完整
- 所有 commit message HEREDOC 完整
- PR body 完整

**3. Type consistency:**
- `capture_fail_open(operation, error, **extra)` 簽章在 Task 1 定義，Task 3/4/5 呼叫均 match ✓
- operation 命名規律：`is_token_revoked` / `rate_limit.postgres_limiter` / `rate_limit.cleanup_buckets` / `rate_limit_db.{function}` — Task 內外一致 ✓
- monkeypatch target 一致：`utils.{module}.capture_fail_open` + `utils.{module}.get_engine` ✓
