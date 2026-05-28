# Spec C: Logger PII redaction (#7)

**日期**：2026-05-28
**狀態**：Draft，等 user 確認
**對應 audit findings**：🔴 P0 #7 — Logger 大量印學生姓名+員工名，log shipping 即外洩
**對應 spec 系列**：A (限流) ✅ / B (CSRF) ✅ / D (audit append-only) ✅ / **C (Logger PII)** / E (LINE 跨境) / F (staff refresh)

---

## 1. Why

### 1.1 攻擊面

`api/students.py:80-85 logger.warning("學生刪除/離園... student_id=%s name=%s", ...)`、`api/auth.py:232 logger.warning("帳號已鎖定: %s", username)`、`services/line_service.py:118-208 build_*_message` 含 student_name 全文。Sentry denylist 只在 event payload 觸發（`_scrub_event` only sees Sentry event），**stdout/file log 完全不過濾**。

風險：log 通常檔案權限寬鬆（運維外包、Logtail / Datadog 等 SaaS log aggregator）等於把 PII 廣播到二級系統。外洩需 72hr 通報主管機關（個資法施行細則 §22）。

### 1.2 既有 PII detection 已就緒

`utils/sentry_init.py` 已有完整 PII 偵測 helper：
- `_PII_KEY_SUBSTRINGS: frozenset` — 60+ keys（salary/student_name/parent_name/phone/email/id_number/medication/...）
- `_PII_KEY_EXEMPT_SUBSTRINGS: frozenset` — 系統欄位例外（ip_address/health_check/email_template/...）
- `_key_is_pii(key: str) -> bool` — exempt-first + substring match
- `_scrub_mapping(obj) -> obj` — 遞迴 dict/list 遮 PII keys
- `_scrub_query_string(value)` — URL query string 內 key 偵測

但**只 process dict/list/mapping**，不對 plain string content 做 PII 偵測。Spec C 需新增 string-level scrubbing。

### 1.3 LogRecord 結構

Python `logging.LogRecord` 含：
- `msg` — format string (e.g. `"學生刪除 student_id=%s name=%s"`)
- `args` — tuple/dict (e.g. `(42, "小明")`)
- `getMessage()` — formatted result (e.g. `"學生刪除 student_id=42 name=小明"`)
- `__dict__` 可能含 extras（custom Logger.addLogRecordExtras）
- `exc_info: tuple[type, Exception, traceback]` — exception 資訊（含 PII SQL value）

Filter 必須對 `getMessage()` 輸出 + `args` 內 dict + `exc_info[1].args` 三處做 redaction。

---

## 2. Goals / Non-goals

### Goals
- (G1) 新 `utils/log_pii_filter.py` 內 `PIIRedactionFilter(logging.Filter)` 重用 sentry `_key_is_pii` + `_scrub_mapping`
- (G2) Filter 內對 `record.msg` **raw format string**（不 call getMessage()）執行 regex 抓 `\w+=\S+` 形式 → key 判 PII → value 替換 `[FILTERED]`
  - **advisor 2026-05-28 修**：原設計 call getMessage() 後 scrub，但 format mismatch 時 try/except swallow 可能讓原 PII msg 漏網。改用 raw record.msg regex scrub，args 不動仍 mutable（args 內 dict 走 _scrub_mapping 已 cover）。
- (G3) Filter 內對 `record.args` 若是 dict 走 `_scrub_mapping`；若是 tuple/list 內 element 是 dict 也 scrub
- (G4) Filter 對 `record.exc_info` traceback 內 SQL value / exception args 做 redact
- (G5) `main.py:_configure_logging` attach 到 root handler（同 `RequestIdLogFilter` pattern，並列在 RequestIdLogFilter 之後）
- (G6) **修 3 處 explicit `name=%s` logger calls** 改用 `student_name=%s`：
  - `api/students.py:82` (學生刪除/離園：自動取消接送通知)
  - `api/students.py:990` (學生離園 lifecycle)
  - `api/students.py:1247` (學生生命週期轉移)
  - **advisor 2026-05-28 抓**：bare `name` 不在 sentry denylist（避免誤殺 entity_name/display_name/course_name 等），filter 不會 redact `name=小明`。修這 3 行改用 explicit `student_name=` key 即可 filter cover。
- (G7) 其餘 logger calls 不改（filter + 修 G6 那 3 行 已 cover audit P0 #7 所列場景）
- (G8) 零回歸：既有 5582 pytest baseline + 6-7 new test 全綠
- (G9) 同步 `_PII_KEY_SUBSTRINGS` 名單與 Sentry 對齊 — import sentry helper 不另抄

### Non-goals
- 不重寫 36+ existing logger calls（filter 自動 strip，避免大規模 churn）
- 不引入 mask 或 hash 模式（純 `[FILTERED]` 替換，與 Sentry `_FILTERED` 一致）
- 不對 GET request URL query string 做額外處理（既有 Sentry 已 cover）
- 不改 Sentry denylist 內容（純複用，新加 PII 欄位仍在 sentry_init.py 改）
- 不在本 spec 內處理 P0/P1 其餘 audit findings（E / F 為獨立 spec）

---

## 3. Architecture

### 3.1 PR 結構（單 PR 1 commit）

| Commit | 範圍 | 檔案數 | 風險 |
|--------|------|--------|------|
| **C1**：`feat(logging): PII redaction filter on root handler` | filter + register + tests | 1 new module + main.py + 1 new test file | 低 |

Spec C scope 小（單一 filter + register + tests），單 PR 1 commit 即可。

### 3.2 PIIRedactionFilter 設計

```python
"""utils/log_pii_filter.py

PII redaction filter for logging records.

對 LogRecord.msg formatted result + record.args (dict) + record.exc_info
做 key-based PII detection 與 value replacement。

重用 utils.sentry_init._key_is_pii 與 _scrub_mapping 保證與 Sentry event
denylist 同步。新增 PII 欄位只改一處 (sentry_init.py _PII_KEY_SUBSTRINGS)。
"""

import logging
import re
from typing import Any

from utils.sentry_init import _key_is_pii, _scrub_mapping, _FILTERED

# 抓 key=value 形式（key 為英數+底線、value 為非空白字串到下一個空白）
# 例：student_id=42 / name=小明 / phone=0912-345-678
# 不抓 key 含空白 / value 跨多 token 的 case（保守 redaction 避免破壞 log debug）
_KEY_VALUE_RE = re.compile(r"(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)=(?P<value>\S+)")


def _redact_string(s: str) -> str:
    """對 string 內所有 key=value pattern，若 key 命中 PII denylist 替換 value 為 [FILTERED]。"""
    def _replace(m: re.Match) -> str:
        key = m.group("key")
        if _key_is_pii(key):
            return f"{key}={_FILTERED}"
        return m.group(0)
    return _KEY_VALUE_RE.sub(_replace, s)


def _redact_args(args: Any) -> Any:
    """對 record.args 做 redaction。
    
    - dict: 走 _scrub_mapping 遞迴遮 PII keys
    - tuple/list: 個別 element 若是 dict 走 _scrub_mapping；其他元素不動
      （format string 的 positional args 沒法跟 key name 對應，
       已透過 _redact_string 對最終 getMessage() 輸出做掃描）
    """
    if isinstance(args, dict):
        return _scrub_mapping(args)
    if isinstance(args, (tuple, list)):
        return type(args)(
            _scrub_mapping(a) if isinstance(a, (dict, list)) else a
            for a in args
        )
    return args


class PIIRedactionFilter(logging.Filter):
    """對 LogRecord 做 PII redaction（msg / args / exc_info 三層）。
    
    Attach 到 logging.root handler (main.py:_configure_logging) 後，所有
    logger 出來的 record 都會走過此 filter，msg 與 args 經 redact 才到 handler。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 1. record.msg raw format string 直接 regex scrub
        # advisor 2026-05-28：不 call getMessage() 避免 format mismatch swallow PII。
        # 對 raw msg scrub 已 cover format string 內的 PII key（key 通常寫在 format
        # string 而非 args 中）；args 仍 mutable 給 handler format，下面 step 2 對
        # args 內 dict 走 _scrub_mapping 補完整。
        if isinstance(record.msg, str):
            redacted = _redact_string(record.msg)
            if redacted != record.msg:
                record.msg = redacted

        # 2. record.args 若是 dict 做 _scrub_mapping
        if record.args:
            record.args = _redact_args(record.args)

        # 3. record.exc_info 含 exception，args 內可能有 PII (e.g. SQL value)
        # 對 exception.args 做 string-level redaction
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            if hasattr(exc, "args") and exc.args:
                try:
                    new_args = tuple(
                        _redact_string(a) if isinstance(a, str) else a
                        for a in exc.args
                    )
                    if new_args != exc.args:
                        exc.args = new_args
                except Exception:
                    pass

        return True  # 不擋 record，純改寫
```

### 3.3 main.py attach filter

```python
# main.py:_configure_logging 內，與 RequestIdLogFilter 並列

def _configure_logging():
    from utils.request_logging import RequestIdLogFilter
    from utils.log_pii_filter import PIIRedactionFilter  # 新加 import

    level = logging.INFO
    rid_filter = RequestIdLogFilter()
    pii_filter = PIIRedactionFilter()  # 新

    # 既有 jsonlogger / basicConfig 路徑都 addFilter 兩個
    # ... (jsonlogger handler.addFilter(rid_filter) 後加 handler.addFilter(pii_filter)) ...
    # ... (basicConfig fallback h.addFilter(rid_filter) 後加 h.addFilter(pii_filter)) ...
```

**順序考量**：filter chain 順序對 record 順序處理。RequestIdLogFilter 加 request_id（不動 msg），PIIRedactionFilter 改 msg。兩者**獨立 不互相 depend**，順序不影響結果。但實作上 PIIRedactionFilter 放後面（先有 request_id 再 redact 不會誤刪 request_id）。

### 3.4 與既有 sentry _scrub_event 互動

- Sentry capture event 走 `before_send=_scrub_event` → event payload 內 PII 被遮
- stdout log 走 PIIRedactionFilter → log line 內 PII 被遮
- **同一條 PII 兩端各自處理**，不互相影響
- 共用 `_key_is_pii` / `_PII_KEY_SUBSTRINGS` → 新增 PII 欄位只需改一處

### 3.5 性能考量

- Filter 對每 record 跑 1-3 個 regex match + sub
- LogRecord 量級：prod ~100-500 lines/sec
- 預期 overhead < 5% latency on log path
- 若 hot path (e.g. salary engine debug log) 性能敏感，可在 spec follow-up 加 `_KEY_VALUE_RE` cache 或 skip DEBUG level

---

## 4. 測試計畫

新增 6 個 pytest 在 `tests/test_log_pii_filter.py`：

1. **test_msg_with_pii_key_value_redacted** — `logger.warning("user student_name=小明 logged in")` → record.msg 變成 `"user student_name=[FILTERED] logged in"`
2. **test_msg_with_non_pii_key_value_kept** — `logger.warning("request request_id=abc123 path=/api/foo")` → request_id (exempt) + path (non-PII) 保留
3. **test_args_dict_with_pii_scrubbed** — `logger.warning("update", extra={"student_name": "小明", "salary": 50000})` → extras dict 內 PII 被遮
4. **test_exc_info_args_redacted** — `try: raise ValueError("phone=0912345678 invalid"); except: logger.exception(...)` → exception.args 內 phone= value 被遮
5. **test_non_string_msg_not_modified** — `logger.warning({"already_dict": True})` → 不報錯，不修改
6. **test_filter_returns_true_does_not_block** — record 通過 filter (return True)，沒被擋

策略：用真實 `logging.Logger` + `StringIO` capture handler，跑 logger.warning 後 inspect captured output。

回歸：跑全套 pytest 5582 baseline。

---

## 5. Roll-out

### 5.1 部署步驟

1. PR 合併（1 commit + 6 new test）。
2. 後端 service 重啟 → filter 立刻生效，所有後續 log 含 PII 即被 redact。
3. 部署後 smoke：
   - 觸發 student 刪除（看 `logger.warning("學生刪除... name=%s")`）→ stdout log 顯示 `name=[FILTERED]`
   - 觸發 login 失敗（看 `logger.warning("帳號已鎖定: %s", username)`）→ 因 username 不在 substring list，保留（**caveat**：username 不被視為 PII，spec follow-up 評估是否加 `username` 進 denylist）

### 5.2 回退方案

純 hotfix revert PR：行為立刻回到「stdout log 含 PII」。零 migration、零 schema。

### 5.3 監控指標

- 7 天觀察 stdout log 是否仍有可識別 PII 漏網
- 評估若 grep 出 `name=<chinese>` / `phone=\d{4}-\d{6}` 等 leak pattern → 補 denylist substring
- 觀察是否 redact 誤殺（e.g. `request_id=abc` 被誤判為 id_number-related）→ 補 exempt substring

---

## 6. 風險與緩解

| 風險 | 影響 | 緩解 |
|------|------|------|
| `_redact_string` regex 對非標準 log format（e.g. `name 小明 student_id 42` 不含 `=`）失效 | 該 log line PII 漏網 | regex 設計刻意保守只抓 `key=value`；高敏感 logger calls 推薦遵循此 format（既有 36+ calls 多數已如此）；漏網 follow-up 加 logger calls 改寫 |
| Filter 對 `record.msg` 改寫後 args 清空，可能影響 logger.error 帶 exception 的 stack trace formatting | exception 仍正常顯示，但 args 不能再用 | filter 只在 msg 真有變更時清 args，無變更時保留 args 原樣 |
| Multi-handler 場景，filter 跑兩次（root handler + child handler 各一個 filter instance） | 第二次 redact 沒效（已 [FILTERED] 不再 match `_key_is_pii`） | filter 設計 idempotent，重複 redact 安全 |
| Sentry _scrub_event 與 PII filter 名單漂移 | 兩端 redact 不一致 | 都 import 同 `_key_is_pii` + `_PII_KEY_SUBSTRINGS`，無漂移可能 |
| `username` 不在 denylist 導致 `logger.warning("帳號已鎖定: %s", username)` 仍 leak | username 是 admin/staff identity 標識 | spec §5.3 monitoring；follow-up 評估是否加 `username` 進 denylist（注意：加了會誤殺所有 `username=<value>` 形式 log 含 audit_log username 欄位） |
| exception.args 改寫可能破壞 exception 自身（exception 物件 mutable but unusual to mutate） | 後續 caller 看 exception.args 為 redacted | 純對 string args redact，list args 不動；改 args 後 raise 仍正常 |

---

## 7. Out of scope

- 不處理 P0/P1 其餘 audit findings（E / F 為獨立 spec）
- 不引入 mask / hash 模式
- 不對 32+ existing logger calls 做手動 refactor
- 不重新設計 sentry denylist 內容
- 不引入 structured log schema validation

---

## 8. 驗收 checklist（user 手測 + roll-out）

PR 合併 + 後端重啟後 USER 手動驗證：

- [ ] 觸發 student 刪除（POST /api/students/{id}/delete-action）→ stdout log 顯示 `student_name=[FILTERED]` 或 `name=[FILTERED]`
- [ ] 觸發 login 失敗 5 次 (現有 audit_login flow) → stdout log 顯示 `username=admin` （username 不被 redact 是預期行為，spec §6 風險）
- [ ] 觸發 LINE 推播（接送通知 / 才藝候補升位）→ stdout log 顯示 `student_name=[FILTERED]`
- [ ] 觸發任意含 traceback 的 exception → 確認 SQL value PII 被遮（如 SQL raise 含 `WHERE phone=...`）
- [ ] Sentry event 仍正常（既有 _scrub_event 沒受影響）
- [ ] pytest 5582 baseline + 6 new test 全綠
- [ ] 抽 5 個 prod log line grep 確認無 chinese name / 數字 phone 在 `*name=` 或 `*phone=` 後面

---

## 9. 後續 follow-up（不在本 spec）

- 評估是否加 `username` 進 denylist（trade-off：audit 紀錄需 username）
- 若 prod log 仍見漏網 PII pattern，補 denylist substring
- 改 36+ logger calls 改用 entity_id only（雙保險）— 屬非 hardening 改善
- 結構化 log schema：強制 logger.info(...,extra={"entity_id": ...}) 避開 free-form msg
