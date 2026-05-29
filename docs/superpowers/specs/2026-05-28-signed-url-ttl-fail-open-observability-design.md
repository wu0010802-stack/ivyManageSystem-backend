# Signed URL TTL 收斂 + Fail-open Observability

**日期**：2026-05-28
**狀態**：Design（pending review）
**Scope**：ivy-backend
**前置任務**：P2 audit finding 20-a (Signed URL TTL 3600s 過長) + 20-e (fail-open 缺 metric)
**相關**：與 sub-project #1 (dependabot) 無耦合
**工時估**：1 天

---

## 1. 動機

### 1.1 Finding 20-a：Signed URL TTL 3600s 過長

請假附件常含診斷書、薪資存摺等敏感檔；Supabase signed URL 預設 TTL 3600s（1 小時）= 連結被攔截重放窗口太長。

### 1.2 Finding 20-e：is_token_revoked + rate-limit fail-open 缺 metric

7 處 fail-open 點（DB 抖動時放行）目前僅 `logger.warning(...)`，無 Sentry alert、無 metric；DB 大範圍失聯時：
- 廢止 token 仍可呼金流端點（auth fail-open）
- Login rate-limit 失效（rate_limit_db fail-open）
- Endpoint rate-limit 失效（rate_limit fail-open）

Ops 完全無感，事後排查只能撈 log。

### 1.3 不做什麼（YAGNI）

- **不改 fail-open 行為**：DB 抖動時繼續放行避免全站 down，這是設計決策不改
- **不導 Prometheus**：無既有 metric backend，純 Sentry tag + capture_exception 即可，Prometheus follow-up
- **不加 rate_limit_capture decorator** 防 Sentry quota 爆：YAGNI；正常情況 fail-open 不該觸發，若噴量爆代表真有事該被看到
- **不改 Signed URL call site code**：3 處 call site 都讀 `settings.storage.supabase_signed_url_ttl`，改 config default 全自動生效

---

## 2. 範圍與整體架構

```
ivy-backend/
├── config/storage.py                ← 修改：TTL default 3600 → 300
├── utils/
│   ├── fail_open.py                 ← 新檔：capture_fail_open helper
│   ├── auth.py                      ← 修改：is_token_revoked 改用 helper（1 處）
│   ├── rate_limit.py                ← 修改：PostgresLimiter + cleanup（2 處）
│   └── rate_limit_db.py             ← 修改：4 處 fail-open
├── tests/
│   ├── test_fail_open.py            ← 新檔：helper 單元測試
│   ├── test_config/test_storage.py  ← 修改：TTL 300 assertion
│   ├── test_jwt_blocklist.py        ← 修改：加 1 條 capture_exception assert
│   └── test_rate_limit*.py          ← 修改：每點加 1 條 capture_exception assert
```

---

## 3. Signed URL TTL 收斂

### 3.1 Config 修改

**檔案：** `config/storage.py`

```python
# 第 23 行
supabase_signed_url_ttl: int = Field(
    default=300,  # 3600 → 300（5 分鐘）。Prod 若 UX 有問題，env override 可暫 revert
    validation_alias="SUPABASE_STORAGE_SIGNED_URL_TTL",
)
```

### 3.2 為何單點修改即覆蓋所有 call site

3 個 call site 都讀 `settings.storage.supabase_signed_url_ttl`，無 hard-code 數字：

| File:Line | 用途 |
|---|---|
| `api/leaves.py:2483-2484` | 教師端請假附件 |
| `api/portal/leaves.py:646-647` | 家長端請假附件 |
| `api/portfolio/reports.py:568-569` | 家長端成長報告 PDF |

第 4 個 reference (`tests/test_storage_backend.py:84` 等)是測試固定值傳入（不讀 config），無需動。

### 3.3 退路

Prod 若實際遇 UX 問題：
- **臨時**：env `SUPABASE_STORAGE_SIGNED_URL_TTL=3600`（或其他值）即時 revert，無 code change
- **永久**：另開 PR 改回 default + 加 per-bucket 細分（leave 300s / report 1800s）

---

## 4. Fail-open Sentry Instrumentation Helper

### 4.1 新檔 `utils/fail_open.py`

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
        **extra: 額外 tag context（如 key、jti、name），會以
            `fail_open.{key}` 形式設成 Sentry tag。**限 primitive value**
            (str/int/bool)；dict/list 等複合型別請呼叫端先序列化避免
            Sentry tag dashboard 顯示 `<repr>`。
    """
    logger.warning("%s 失敗，fail-open: %s", operation, error)
    with sentry_sdk.push_scope() as scope:
        scope.set_tag("fail_open", operation)
        for k, v in extra.items():
            scope.set_tag(f"fail_open.{k}", str(v))
        sentry_sdk.capture_exception(error)
```

### 4.2 設計理由

- **單一 entry point**：所有 fail-open 點走同一 helper，將來改 Sentry payload schema 只動 1 處
- **保留 `logger.warning`**：對齊既有 log retention 與 grep workflow（既有 ops 流程不變）
- **`push_scope`**：tag 只 attach 該 event 不污染全 scope（對齊 `utils/exception_handlers.py:88-96` pattern）
- **tag prefix `fail_open.`**：Sentry dashboard 用 `fail_open:*` 或 `fail_open.scope:login` 過濾
- **`operation` 字串穩定**：命名規律固定，作為 Sentry alert rule key
- **`extra` 限 stringify**：避免 PII 經由 tag 洩漏（PII 已被 sentry_init `_scrub_event` 處理；tag 端額外用 `str(v)` 保險）

---

## 5. 7 個 Fail-open Call Site 改造

### 5.1 改造對照表（完整）

| # | File:Line | Function | helper call |
|---|---|---|---|
| 1 | utils/auth.py:240 | `is_token_revoked` | `capture_fail_open("is_token_revoked", e, jti=jti)` |
| 2 | utils/rate_limit.py:145 | `PostgresLimiter.check` | `capture_fail_open("rate_limit.postgres_limiter", e, name=self.name, key=key)` |
| 3 | utils/rate_limit.py:212 | `cleanup_rate_limit_buckets` | `capture_fail_open("rate_limit.cleanup_buckets", e)` |
| 4 | utils/rate_limit_db.py:99 | `bump_failed_login` | `capture_fail_open("rate_limit_db.bump_failed_login", e, key=key)` |
| 5 | utils/rate_limit_db.py:134 | `bump_login_streak` | `capture_fail_open("rate_limit_db.bump_login_streak", e, key=key)` |
| 6 | utils/rate_limit_db.py:157 | `clear_login_failures` | `capture_fail_open("rate_limit_db.clear_login_failures", e, key=key)` |
| 7 | utils/rate_limit_db.py:190 | `earliest_attempt_at` | `capture_fail_open("rate_limit_db.earliest_attempt_at", e, scope=scope, key=key)` |

### 5.2 改造 diff pattern

每個 call site 同樣模式（以 #1 為範例）：

```diff
+from utils.fail_open import capture_fail_open

 def is_token_revoked(jti: str) -> bool:
     ...
-    except Exception as e:
-        logger.warning("is_token_revoked 查詢失敗，fail-open: %s", e)
-        return False
+    except Exception as e:
+        capture_fail_open("is_token_revoked", e, jti=jti)
+        return False
```

**所有 7 處同樣 surgical 改動**：只替換 `logger.warning(...)` 那一行，return 值與 except 結構不變。fail-open 行為 100% 等同。

### 5.3 Operation 命名規律

| Pattern | 範例 |
|---|---|
| `{module_basename}.{function}` | `rate_limit_db.bump_failed_login` |
| 單一 module 已 self-evident 可省 | `is_token_revoked`（utils/auth.py 唯一 fail-open） |

Sentry alert rule 例：
- `fail_open:is_token_revoked count() > 5 in 5min` → DB 連線抖動 alert
- `fail_open:rate_limit_db.* count() > 20 in 5min` → login 路徑 DB 異常

---

## 6. 測試

### 6.1 新檔 `tests/test_fail_open.py`

```python
"""capture_fail_open helper 單元測試。"""
from unittest.mock import MagicMock, patch

import pytest

from utils.fail_open import capture_fail_open


def test_capture_fail_open_logs_warning(caplog):
    """應 log warning 含 operation 名稱與 error。"""
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

    with patch("utils.fail_open.sentry_sdk.push_scope", return_value=fake_scope_cm) as mock_push, \
         patch("utils.fail_open.sentry_sdk.capture_exception") as mock_capture:
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

    with patch("utils.fail_open.sentry_sdk.push_scope", return_value=fake_scope_cm), \
         patch("utils.fail_open.sentry_sdk.capture_exception"):
        capture_fail_open("rate_limit.check", RuntimeError("x"), scope="login", key="ip:1.2.3.4", count=42)
        fake_scope.set_tag.assert_any_call("fail_open.scope", "login")
        fake_scope.set_tag.assert_any_call("fail_open.key", "ip:1.2.3.4")
        fake_scope.set_tag.assert_any_call("fail_open.count", "42")  # int → "42"


def test_capture_fail_open_no_extra_works():
    """無 extra kwargs 也應正常 capture。"""
    fake_scope = MagicMock()
    fake_scope_cm = MagicMock()
    fake_scope_cm.__enter__ = MagicMock(return_value=fake_scope)
    fake_scope_cm.__exit__ = MagicMock(return_value=False)

    with patch("utils.fail_open.sentry_sdk.push_scope", return_value=fake_scope_cm), \
         patch("utils.fail_open.sentry_sdk.capture_exception") as mock_capture:
        capture_fail_open("op", RuntimeError("x"))
        mock_capture.assert_called_once()
```

### 6.2 既有測試擴充

每個 7 處 fail-open call site 既有 test 加 1 條 regression：

**範例 `tests/test_jwt_blocklist.py`：**
```python
def test_is_token_revoked_db_failure_calls_capture_fail_open(monkeypatch):
    """DB raise 時應 capture_fail_open + 仍回 False（fail-open 行為保留）。"""
    calls = []

    def fake_capture(operation, error, **extra):
        calls.append((operation, type(error).__name__, extra))

    monkeypatch.setattr("utils.auth.capture_fail_open", fake_capture)

    # mock get_engine().connect() raise
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

**Rate-limit 既有 test 同模式擴充 6 處**（每個 fail-open 點 1 條）。

### 6.3 Config test 更新

**`tests/test_config/test_storage.py:22`：**
```diff
 def test_default_settings():
     s = StorageSettings()
-    assert s.supabase_signed_url_ttl == 3600
+    assert s.supabase_signed_url_ttl == 300
```

對應 line 35 env override test 不動（仍 assert env value `7200`）。

---

## 7. 行為變更與 User 影響

### 7.1 Signed URL TTL 縮短

| 場景 | 既有行為 (3600s) | 新行為 (300s) |
|---|---|---|
| HR 點請假審核連結 → 看附件 | 1 hr 內隨時可看 | 5 min 內看；超過要重 fetch URL（自動，user 重點審核連結即可） |
| 家長端打開請假申請 → 看自己上傳附件 | 1 hr 內 | 5 min 內 |
| 家長端打開成長報告 PDF | 1 hr 內 | 5 min 內；下載完之後檔在本機可看（無時限） |

**估** UX 影響：低（user 通常一打開連結立刻看完）。

**Prod rollback：** env `SUPABASE_STORAGE_SIGNED_URL_TTL=3600` 即時 revert。

### 7.2 Fail-open observability

| 場景 | 既有行為 | 新行為 |
|---|---|---|
| DB 抖動 1s（zero impact） | log warning | log warning + Sentry 1 event |
| DB 失聯 5 min | log warning N 次 | log warning N 次 + Sentry N events + tag filter 立刻 alert |

**Sentry quota impact：** 估每月 +50-200 events（DB 抖動是少數，正常情況零）。若噴量爆 (>500/day) 表示真有 incident，正是 Sentry 該抓的價值。

---

## 8. Prerequisites

無 user 手動操作。Sentry 設定既有（CLAUDE.md §「Sentry 錯誤監控」），缺 SENTRY_DSN 時 helper 自動 no-op（既有 sentry_init 行為）。

---

## 9. Out of Scope（follow-up）

| Follow-up | 屬性 | 何時做 |
|---|---|---|
| Prometheus metric counter | 量化趨勢 | 若導入 Prometheus infrastructure 後 |
| `rate_limit_capture` decorator | 防 Sentry quota 爆 | 若觀察期見 fail-open spam (>500/day) |
| Per-bucket TTL（leave 300s / report 1800s） | UX 細分 | 若 prod 反饋 growth report 5min 不夠 |
| Sentry alert rule 設定 | ops 流程 | merge 後 user 手動到 Sentry dashboard 設 alert |

---

## 10. 風險與回退

### 10.1 主要風險

- **TTL 收緊 → user 看附件時 URL 過期**：mitigation env override 即時 revert
- **`capture_fail_open` import 循環**：utils/fail_open.py 只 import `sentry_sdk` + `logging`，無 model/config 依賴；其他 utils 反向 import 它安全
- **既有 7 處 fail-open test 改 mock 失敗**：mitigation 用 `monkeypatch.setattr("module.path.capture_fail_open", ...)` pattern；既有 mock get_engine 不變

### 10.2 回退方式

- **完整回退**：revert PR；7 處 fail-open 改回 logger.warning（mechanical）
- **只回退 TTL**：env `SUPABASE_STORAGE_SIGNED_URL_TTL=3600`
- **只關 fail-open Sentry**：暫時把 `capture_fail_open` body 中 `sentry_sdk.capture_exception(error)` 註解掉

---

## 11. 預估與分工

- **規模**：1 新檔 (`utils/fail_open.py` ~30 行) + 4 修改檔（config/storage.py 1 行 / utils/auth.py 1 處 / utils/rate_limit.py 2 處 / utils/rate_limit_db.py 4 處）+ 1 新 test 檔 + 既有 test 7+1 擴充
- **工時**：1 天（含 spec / plan / commit / push / PR）
- **PR 數**：1（backend only）
- **依賴**：無（與 sub-project #1 dependabot 獨立）
- **block 後續 sub-project？** 不 block；#3-6 都可獨立
