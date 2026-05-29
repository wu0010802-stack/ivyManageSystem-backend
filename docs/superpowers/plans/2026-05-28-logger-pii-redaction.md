# Logger PII redaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** 加 `PIIRedactionFilter` 對所有 stdout/file log strip PII（重用 sentry _key_is_pii 保證同步）+ 修 api/students.py 3 處 `name=%s` → `student_name=%s` 讓 filter 命中。

**Architecture:** 單 PR 2 commit：(C1) filter module + main.py register + 6 pytest / (C2) api/students.py 3 行 logger calls rename。

**Tech Stack:** Python logging.Filter / regex string scrub / sentry_init helper reuse

**Spec:** `docs/superpowers/specs/2026-05-28-logger-pii-redaction-design.md` (commit `eba21c9`)

---

## File Structure

**New files:**
- `utils/log_pii_filter.py` — `PIIRedactionFilter(logging.Filter)` + `_redact_string` / `_redact_args` helpers
- `tests/test_log_pii_filter.py` — 6 pytest cover msg / args / exc_info / bypass / idempotent / filter return True

**Modified files:**
- `main.py` — `_configure_logging` 內 addFilter(PIIRedactionFilter) 並列 RequestIdLogFilter
- `api/students.py` — 3 處 `name=%s` → `student_name=%s` (line 82 / 990 / 1247)

**Unchanged but referenced:**
- `utils/sentry_init.py:28 _PII_KEY_SUBSTRINGS` + `:86 _PII_KEY_EXEMPT_SUBSTRINGS` + `:128 _key_is_pii` + `_scrub_mapping` — 完整重用
- `utils/request_logging.py:31 RequestIdLogFilter` — filter pattern 範本

---

## Task 1: PIIRedactionFilter + register + 6 pytest

**Files:**
- Create: `utils/log_pii_filter.py`
- Modify: `main.py`
- Create: `tests/test_log_pii_filter.py`

### Steps

- [ ] **Step 1.1: 建 utils/log_pii_filter.py**

完整代碼 per Spec §3.2，但對 `filter()` method 用 raw msg regex（不 call getMessage()，advisor v2 fix）：

```python
"""utils/log_pii_filter.py

PII redaction filter for logging records. 重用 sentry _key_is_pii。
"""

import logging
import re
from typing import Any

from utils.sentry_init import _key_is_pii, _scrub_mapping, _FILTERED

# 抓 key=value 形式（key 為英數+底線、value 為非空白/非中文標點字串）
_KEY_VALUE_RE = re.compile(r"(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)=(?P<value>[^\s,，。]+)")


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
    - tuple/list: element 若 dict 走 _scrub_mapping；其他元素不動
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
    """對 LogRecord 做 PII redaction（msg / args / exc_info 三層）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # 1. record.msg raw format string 直接 regex scrub
        if isinstance(record.msg, str):
            redacted = _redact_string(record.msg)
            if redacted != record.msg:
                record.msg = redacted

        # 2. record.args 若是 dict/list 做 _scrub_mapping
        if record.args:
            record.args = _redact_args(record.args)

        # 3. exc_info exception args 做 string redact
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

        return True  # 不擋 record
```

- [ ] **Step 1.2: main.py attach filter**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-logger-pii-redaction-2026-05-28-backend
sed -n '121,165p' main.py
```

找 `_configure_logging` 內 `rid_filter = RequestIdLogFilter()` 那行後加 `pii_filter = PIIRedactionFilter()`，並在每處 `handler.addFilter(rid_filter)` 之後加 `handler.addFilter(pii_filter)`：

```python
def _configure_logging():
    from utils.request_logging import RequestIdLogFilter
    from utils.log_pii_filter import PIIRedactionFilter

    level = logging.INFO
    rid_filter = RequestIdLogFilter()
    pii_filter = PIIRedactionFilter()

    # ... jsonlogger path ...
    handler.addFilter(rid_filter)
    handler.addFilter(pii_filter)
    # ... basicConfig fallback path 兩處同樣 addFilter ...
```

- [ ] **Step 1.3: 建 tests/test_log_pii_filter.py**

```python
"""Spec C: PIIRedactionFilter 6 pytest。"""

import io
import logging

import pytest

from utils.log_pii_filter import PIIRedactionFilter


@pytest.fixture
def logger_with_filter():
    """建獨立 logger 加 PIIRedactionFilter + StringIO handler 捕捉輸出。"""
    log = logging.getLogger("test_pii_filter")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(PIIRedactionFilter())
    log.addHandler(handler)

    yield log, stream
    log.handlers.clear()


def test_msg_with_pii_key_value_redacted(logger_with_filter):
    """student_name=小明 → student_name=[FILTERED]。"""
    log, stream = logger_with_filter
    log.warning("user student_name=小明 logged in")
    out = stream.getvalue()
    assert "student_name=[Filtered]" in out
    assert "小明" not in out


def test_msg_with_non_pii_key_value_kept(logger_with_filter):
    """request_id (exempt) + student_id (non-PII) 保留。"""
    log, stream = logger_with_filter
    log.warning("request request_id=abc123 student_id=42 path=/api/foo")
    out = stream.getvalue()
    assert "request_id=abc123" in out
    assert "student_id=42" in out
    assert "[Filtered]" not in out


def test_args_dict_with_pii_scrubbed(logger_with_filter):
    """logger.warning(msg, dict_args) 內 dict PII keys 被遮。"""
    log, stream = logger_with_filter
    log.warning("update %(student_name)s %(salary)s", {"student_name": "小明", "salary": 50000})
    out = stream.getvalue()
    assert "小明" not in out
    assert "50000" not in out
    assert "[Filtered]" in out


def test_exc_info_args_redacted(logger_with_filter):
    """exception args 內 PII 被遮。"""
    log, stream = logger_with_filter
    try:
        raise ValueError("phone=0912345678 invalid")
    except ValueError:
        log.exception("validation failed")
    out = stream.getvalue()
    assert "0912345678" not in out


def test_non_string_msg_not_modified(logger_with_filter):
    """logger.warning(dict 物件) → 不報錯，不修改 dict (filter 只處理 str msg)。"""
    log, stream = logger_with_filter
    log.warning({"already_dict": True})
    # Should not raise


def test_filter_returns_true_does_not_block(logger_with_filter):
    """Filter return True，record 通過不被擋。"""
    log, stream = logger_with_filter
    log.warning("test message")
    assert "test message" in stream.getvalue()
```

注意 `_FILTERED` constant 在 sentry_init.py 內值可能是 `"[Filtered]"` 或 `"[FILTERED]"`，assert 對齊實際值（grep `_FILTERED =` utils/sentry_init.py 確認）。

- [ ] **Step 1.4: 跑 PII filter test**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-logger-pii-redaction-2026-05-28-backend
pytest tests/test_log_pii_filter.py -v 2>&1 | tail -15
```
Expected: 6 pass。如 fail，調 `_FILTERED` constant string 對齊。

- [ ] **Step 1.5: 跑全套 pytest sample 確認零回歸**

```bash
pytest tests/test_log_pii_filter.py tests/test_employees.py tests/test_auth.py -v --tb=line 2>&1 | tail -10
```
Expected: 6 new test + 既有 audit test 全綠。

- [ ] **Step 1.6: Commit (C1)**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-logger-pii-redaction-2026-05-28-backend
git add utils/log_pii_filter.py main.py tests/test_log_pii_filter.py
git commit -m "$(cat <<'EOF'
feat(logging): PII redaction filter on root handler

Spec C (audit P0 #7)：PIIRedactionFilter 對所有 stdout/file log strip PII。

設計：
- 重用 utils.sentry_init._key_is_pii + _scrub_mapping + _PII_KEY_SUBSTRINGS
  保證與 Sentry event denylist 同步，新增 PII 欄位只改 sentry_init.py
- record.msg raw regex scrub（不 call getMessage() 避免 format mismatch
  swallow PII）+ record.args dict scrub + exc_info args redact 三層
- main.py:_configure_logging attach 同 RequestIdLogFilter pattern (jsonlogger
  + basicConfig fallback 三處 handler 全 addFilter)
- 6 個 pytest cover：msg redact / non-PII keep / args dict scrub /
  exception args / non-string msg / return True

Refs: Spec docs/superpowers/specs/2026-05-28-logger-pii-redaction-design.md §2-3
EOF
)"
```

---

## Task 2: 修 api/students.py 3 處 name=%s → student_name=%s

**Files:**
- Modify: `api/students.py:82, 990, 1247`

### Steps

- [ ] **Step 2.1: grep 確認 3 處位置**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-logger-pii-redaction-2026-05-28-backend
grep -n "name=%s" api/students.py
```
Expected: 3 處 line number。

- [ ] **Step 2.2: 3 處 inline 修改**

對每處讀上下文確認是 student.name 而非其他 entity name，然後改：
- `name=%s` → `student_name=%s`
- format args 保持不變（仍傳 student.name 之類）

例（line 82）：
```python
# Before
"學生刪除/離園：自動取消 %d 筆進行中接送通知，student_id=%s name=%s"
# After  
"學生刪除/離園：自動取消 %d 筆進行中接送通知，student_id=%s student_name=%s"
```

對齊 3 處全改。

- [ ] **Step 2.3: 跑 students.py 相關 test 確認沒破 format**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-logger-pii-redaction-2026-05-28-backend
pytest tests/test_students*.py -v --tb=line 2>&1 | tail -10
```
Expected: 全綠 (format 改成 student_name= 不影響 args 數量)。

- [ ] **Step 2.4: Commit (C2)**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-logger-pii-redaction-2026-05-28-backend
git add api/students.py
git commit -m "$(cat <<'EOF'
fix(students): logger calls use student_name= for PII filter coverage

3 處 logger calls 從 name=%s 改 student_name=%s 讓 Task 1 PIIRedactionFilter
能命中（bare 'name' 不在 sentry denylist 避免誤殺 entity_name/display_name/
course_name 等）。

- api/students.py:82 學生刪除/離園自動取消接送通知
- api/students.py:990 學生離園 lifecycle 完成
- api/students.py:1247 學生生命週期轉移

Refs: Spec docs/superpowers/specs/2026-05-28-logger-pii-redaction-design.md §2 G6
EOF
)"
```

---

## Task 3: 最終驗收 + push branch

### Steps

- [ ] **Step 3.1: 全套 pytest（background ~22-40 min）**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-logger-pii-redaction-2026-05-28-backend
pytest --tb=short 2>&1 | tail -10
```

- [ ] **Step 3.2: git log 確認 commit 結構**

```bash
git log --oneline origin/main..HEAD
git diff origin/main..HEAD --stat
```
Expected:
- 3 commits (spec + 2 implementation)
- 4 files: spec/plan/utils/log_pii_filter.py/main.py/api/students.py/tests/test_log_pii_filter.py

- [ ] **Step 3.3: push worktree branch**

```bash
git push -u origin feat/logger-pii-redaction-2026-05-28-backend
```

- [ ] **Step 3.4: 報告完成**

向 user 回報：
- ✅ Branch pushed
- ✅ Roll-out checklist (spec §8)
- 提醒：username 不 redact 是 by-design（audit log 需要）

---

## Spec Coverage Check

| Spec section | Task | Status |
|--------------|------|--------|
| §2 G1 PIIRedactionFilter | Task 1 Step 1.1 | ✓ |
| §2 G2 raw msg regex scrub | Task 1 Step 1.1 (filter method) | ✓ |
| §2 G3 record.args scrub | Task 1 Step 1.1 | ✓ |
| §2 G4 exc_info args redact | Task 1 Step 1.1 | ✓ |
| §2 G5 main.py attach filter | Task 1 Step 1.2 | ✓ |
| §2 G6 修 api/students.py 3 處 | Task 2 | ✓ |
| §2 G8 零回歸 | Task 1 Step 1.5 + Task 3 | ✓ |
| §2 G9 重用 sentry helper | Task 1 Step 1.1 import | ✓ |
| §3 完整 filter design | Task 1 Step 1.1 | ✓ |
| §4 6 個 pytest | Task 1 Step 1.3 | ✓ |
