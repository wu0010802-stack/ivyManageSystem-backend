# 考勤異動申請共用框架整合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 抽 7 個共用 helper（升級 2 個既有 + 新增 5 個），統一 `leaves` / `overtimes` / `punch_corrections` 三 router 的審核日誌、封存守衛、列鎖、批次提交、代理人、LINE 通知、Excel 匯入邏輯。

**Architecture:** 兩階段 stacked。Stage 1（commits 1-4）為**純抽取**：升級 `utils/approval_helpers.py` + `services/salary/utils.py` 既有共用 helper，新增 `services/salary/finalize_guard.py` 與 `services/notification/approval_notifier.py`，三 router 改 import。Stage 2（commits 5-9）為**行為層整合**：抽 batch_executor 兩段提交、新增 cross-type offset（feature flag）、Excel 匯入骨架、清理 deprecated。每 commit 獨立可 revert。

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic, pytest, PostgreSQL（advisory lock）

**Spec:** `docs/superpowers/specs/2026-05-13-attendance-request-consolidation-design.md`

**Branch strategy:**
- Stage 1 → `refactor/attendance-consolidation-stage1`（commits 1-4，先 merge）
- Stage 2 → `refactor/attendance-consolidation-stage2`（commits 5-9，stacked on stage1）

---

## File Structure

**Stage 1：**
- Modify: `utils/approval_helpers.py` — 升級 `_write_approval_log` 加 `metadata` JSON 參數，改 keyword-only
- Create: `services/salary/finalize_guard.py` — 抽 leaves 的 `_check_salary_months_not_finalized` 推廣到 ot/pc
- Modify: `services/salary/utils.py` — `lock_and_premark_stale` 包成 context manager 包裝（保留原函式以兼容）
- Create: `services/notification/__init__.py` + `services/notification/approval_notifier.py` — 統一 LINE 通知入口
- Modify: `api/leaves.py`, `api/overtimes.py`, `api/punch_corrections.py` — 改用上述 helper
- Create: `tests/test_approval_log_writer_consolidation.py`
- Create: `tests/test_finalize_guard.py`
- Create: `tests/test_stale_marker_context.py`
- Create: `tests/test_approval_notifier.py`

**Stage 2：**
- Create: `services/approval/__init__.py` + `services/approval/batch_executor.py`
- Create: `services/approval/delegate.py`
- Create: `utils/excel_io.py`
- Modify: `api/leaves.py`, `api/overtimes.py`, `api/punch_corrections.py` — 改用 batch_executor
- Modify: `api/leaves.py` — 接 cross-type offset（feature flag gated）
- Modify: `api/leaves.py` — Excel 匯入改用 excel_io
- Create: `tests/test_batch_executor.py`
- Create: `tests/test_delegate_resolver.py`
- Create: `tests/test_cross_type_offset.py`
- Create: `tests/test_excel_io.py`
- Create: `RELEASE_NOTES.md`（or 既有 release log 加區段）

---

## Stage 1 — 純抽取（commits 1-4）

### Task 1: Commit 1 — 升級 `_write_approval_log` 加 metadata

**Goal:** 把 `utils/approval_helpers.py:_write_approval_log` 改為 keyword-only + 加 `metadata: dict | None` 參數，metadata 序列化到 `ApprovalLog.comment` 的尾段（保留 `ApprovalLog` schema 不動）或新增 `metadata` JSONB 欄位。**選最小變更**：metadata 序列化進 comment（JSON 字串），不動 DB schema。

**Files:**
- Modify: `utils/approval_helpers.py:82-117`
- Modify: `api/leaves.py:1491`, `api/leaves.py:1876`, `api/overtimes.py:188`, `api/overtimes.py:1416`, `api/overtimes.py:1619`, `api/punch_corrections.py:156`, `api/punch_corrections.py:245`
- Create: `tests/test_approval_log_writer_consolidation.py`

#### Step 1.1: 先確認分支

```bash
cd ~/Desktop/ivy-backend
git checkout main && git pull
git checkout -b refactor/attendance-consolidation-stage1
```

- [ ] **Step 1.2: Write failing test**

Create `tests/test_approval_log_writer_consolidation.py`:

```python
"""驗證升級後的 _write_approval_log：keyword-only + metadata 序列化。"""
import json

import pytest
from utils.approval_helpers import _write_approval_log
from models.database import ApprovalLog


def test_write_approval_log_with_metadata_serializes_into_comment(db_session):
    log = _write_approval_log(
        session=db_session,
        doc_type="leave",
        doc_id=999,
        action="approve",
        approver={"id": 1, "username": "admin", "role": "admin"},
        comment="ok",
        metadata={"delegate_id": 42, "cross_offset_ot_id": None},
    )
    assert log is not None
    assert log.comment is not None
    # metadata 嵌在 comment 的後綴（[META] JSON 標記）
    assert "[META]" in log.comment
    payload = json.loads(log.comment.split("[META]", 1)[1])
    assert payload == {"delegate_id": 42, "cross_offset_ot_id": None}


def test_write_approval_log_without_metadata_backward_compatible(db_session):
    log = _write_approval_log(
        session=db_session,
        doc_type="overtime",
        doc_id=1,
        action="reject",
        approver={"id": 2, "username": "manager", "role": "supervisor"},
        comment="overlap",
    )
    assert log is not None
    assert log.comment == "overlap"


def test_write_approval_log_positional_rejected():
    """改 keyword-only 後，舊 positional 呼叫應拋 TypeError"""
    with pytest.raises(TypeError):
        _write_approval_log(  # type: ignore[call-arg]
            "leave", 1, "approve", {"id": 1}, "ok", None,
        )
```

- [ ] **Step 1.3: Run test — expect FAIL**

```bash
pytest tests/test_approval_log_writer_consolidation.py -v
```
Expected: 3 tests FAIL（`metadata` kwarg unknown / positional ok）

- [ ] **Step 1.4: Update `utils/approval_helpers.py:82-117`**

Replace `_write_approval_log`:

```python
import json as _json


def _write_approval_log(
    *,
    session,
    doc_type: str,
    doc_id: int,
    action: str,
    approver: dict,
    comment: str | None = None,
    metadata: dict | None = None,
):
    """寫入簽核記錄並回傳 row（含 id）。

    Why keyword-only: 三個 router 共用此 helper，metadata 為新增欄位，強制 keyword 呼叫避免位置混淆。
    Why metadata-in-comment: 不動 ApprovalLog schema（migration 風險），用 `[META]` 分隔符嵌入 comment 尾段；
        前端僅顯示 `[META]` 前段，metadata 留給 audit/report 解析。
    """
    try:
        full_comment = comment or ""
        if metadata:
            payload = _json.dumps(metadata, ensure_ascii=False, sort_keys=True)
            sep = "\n" if full_comment else ""
            full_comment = f"{full_comment}{sep}[META]{payload}"
        log = ApprovalLog(
            doc_type=doc_type,
            doc_id=doc_id,
            action=action,
            approver_id=approver.get("id"),
            approver_username=approver.get("username", ""),
            approver_role=approver.get("role", ""),
            comment=full_comment or None,
        )
        session.add(log)
        session.flush()
        return log
    except Exception as exc:
        logger.warning(
            "審核日誌寫入失敗（%s #%d action=%s operator=%s）：%s",
            doc_type, doc_id, action, approver.get("username", "unknown"), exc,
        )
        return None
```

- [ ] **Step 1.5: Update 7 call sites — 改 keyword-only**

For each of these call sites, change positional args to keyword:
- `api/leaves.py:1491` (look for `_write_approval_log(`)
- `api/leaves.py:1876`
- `api/overtimes.py:188`
- `api/overtimes.py:1416`
- `api/overtimes.py:1619`
- `api/punch_corrections.py:156`
- `api/punch_corrections.py:245`

Example transformation:
```python
# Before
_write_approval_log("leave", leave.id, "approve", approver_info, comment, session)
# After
_write_approval_log(
    session=session,
    doc_type="leave",
    doc_id=leave.id,
    action="approve",
    approver=approver_info,
    comment=comment,
)
```

For leaves where there's a `delegate_id` available locally, add `metadata={"delegate_id": delegate_id}` (only at leave approve sites — `api/leaves.py:1491`).

- [ ] **Step 1.6: Run new test + full suite**

```bash
pytest tests/test_approval_log_writer_consolidation.py -v
pytest -x  # 全測試
```
Expected: 3 new tests PASS, no regression.

- [ ] **Step 1.7: Commit**

```bash
git add utils/approval_helpers.py api/leaves.py api/overtimes.py api/punch_corrections.py tests/test_approval_log_writer_consolidation.py
git commit -m "$(cat <<'EOF'
refactor(approval): _write_approval_log 升級 keyword-only + metadata

加 metadata JSON 參數，序列化進 comment 尾段（不動 schema）。三 router 改 keyword 呼叫。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Commit 2 — 新增 `services/salary/finalize_guard.py`

**Goal:** 把 `api/leaves.py:98-138` 的 `_check_salary_months_not_finalized` + `_collect_leave_months` 抽到共用位置，並推廣為支援 overtime / punch_correction（單日 target_date）。三 router 改 import。

**Files:**
- Create: `services/salary/finalize_guard.py`
- Modify: `api/leaves.py:98-150`（刪除舊 helper）, `api/leaves.py:992`, `api/leaves.py:1131`, `api/leaves.py:1438`, `api/leaves.py:1785`
- Modify: `api/overtimes.py`（接上 single-day check）
- Modify: `api/punch_corrections.py`（接上 single-day check）
- Create: `tests/test_finalize_guard.py`

- [ ] **Step 2.1: Write failing test**

Create `tests/test_finalize_guard.py`:

```python
"""驗證 finalize_guard：跨月、單日、空集合、finalized 偵測。"""
from datetime import date

import pytest
from fastapi import HTTPException

from services.salary.finalize_guard import (
    collect_months_from_range,
    collect_months_from_dates,
    assert_months_not_finalized,
)


def test_collect_months_from_range_single_month():
    months = collect_months_from_range(date(2026, 5, 1), date(2026, 5, 31))
    assert months == {(2026, 5)}


def test_collect_months_from_range_cross_two_months():
    months = collect_months_from_range(date(2026, 5, 28), date(2026, 6, 2))
    assert months == {(2026, 5), (2026, 6)}


def test_collect_months_from_range_cross_year():
    months = collect_months_from_range(date(2025, 12, 30), date(2026, 1, 5))
    assert months == {(2025, 12), (2026, 1)}


def test_collect_months_from_dates_single_day():
    months = collect_months_from_dates([date(2026, 5, 15)])
    assert months == {(2026, 5)}


def test_collect_months_from_dates_multiple():
    months = collect_months_from_dates([date(2026, 5, 15), date(2026, 6, 1)])
    assert months == {(2026, 5), (2026, 6)}


def test_assert_months_not_finalized_empty_set_is_noop(db_session):
    assert_months_not_finalized(db_session, employee_id=1, months=set())


def test_assert_months_not_finalized_raises_when_any_finalized(
    db_session, finalized_salary_record_factory
):
    finalized_salary_record_factory(employee_id=1, year=2026, month=5)
    with pytest.raises(HTTPException) as exc:
        assert_months_not_finalized(
            db_session, employee_id=1, months={(2026, 5), (2026, 6)}
        )
    assert exc.value.status_code == 409
    assert "2026 年 5 月" in exc.value.detail


def test_assert_months_not_finalized_passes_when_none_finalized(db_session):
    assert_months_not_finalized(
        db_session, employee_id=1, months={(2026, 7), (2026, 8)}
    )
```

> **Note:** `finalized_salary_record_factory` 是 conftest fixture，先檢查 `tests/conftest.py` 是否已有；若無，加入：
> ```python
> @pytest.fixture
> def finalized_salary_record_factory(db_session):
>     def _create(*, employee_id, year, month):
>         from models.database import SalaryRecord
>         rec = SalaryRecord(
>             employee_id=employee_id, salary_year=year, salary_month=month,
>             is_finalized=True, finalized_by="test",
>         )
>         db_session.add(rec)
>         db_session.flush()
>         return rec
>     return _create
> ```

- [ ] **Step 2.2: Run test — expect FAIL**

```bash
pytest tests/test_finalize_guard.py -v
```
Expected: All FAIL (`services.salary.finalize_guard` not found)

- [ ] **Step 2.3: Create `services/salary/finalize_guard.py`**

```python
"""跨月份/單日封存守衛 — leaves/overtimes/punch_corrections 共用。

Why this module: 原本 `_check_salary_months_not_finalized` 與 `_collect_leave_months`
僅在 api/leaves.py 內，overtimes/punch_corrections 各自重寫單月判斷。
抽到此處後三個 router 統一呼叫，確保新增封存行為一致同步。
"""
from datetime import date
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy import and_, or_

from models.database import SalaryRecord


def collect_months_from_range(start_date: date, end_date: date) -> set[tuple[int, int]]:
    """收集 [start_date, end_date] 跨越的所有 (year, month)。用於 leave 跨月假單。"""
    months: set[tuple[int, int]] = set()
    current = start_date.replace(day=1)
    end_first = end_date.replace(day=1)
    while current <= end_first:
        months.add((current.year, current.month))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)
    return months


def collect_months_from_dates(dates: Iterable[date]) -> set[tuple[int, int]]:
    """收集多個單日所屬的 (year, month)。用於 overtime / punch_correction。"""
    return {(d.year, d.month) for d in dates}


def assert_months_not_finalized(
    session, *, employee_id: int, months: set[tuple[int, int]]
) -> None:
    """commit 前的封存保護守衛。任一月份已封存即 raise HTTPException(409)。"""
    if not months:
        return
    record = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == employee_id,
            SalaryRecord.is_finalized == True,
            or_(
                *(
                    and_(SalaryRecord.salary_year == yr, SalaryRecord.salary_month == mo)
                    for yr, mo in months
                )
            ),
        )
        .first()
    )
    if record:
        by = record.finalized_by or "系統"
        raise HTTPException(
            status_code=409,
            detail=(
                f"{record.salary_year} 年 {record.salary_month} 月薪資已封存（結算人：{by}），"
                "無法修改該月份的記錄。請先至薪資管理頁面解除封存後再操作。"
            ),
        )
```

- [ ] **Step 2.4: Run new tests — expect PASS**

```bash
pytest tests/test_finalize_guard.py -v
```
Expected: All 8 tests PASS.

- [ ] **Step 2.5: Update `api/leaves.py` — 改用新 helper**

In `api/leaves.py`:
1. Add import at top (around existing `from services.salary.utils import ...`):
   ```python
   from services.salary.finalize_guard import (
       collect_months_from_range,
       assert_months_not_finalized,
   )
   ```
2. **Delete** `_check_salary_months_not_finalized` (lines 98-136) and `_collect_leave_months` (lines 138-148).
3. Replace 4 call sites:
   - `api/leaves.py:992`: `_check_salary_months_not_finalized(session, employee_id, leave_months)` → `assert_months_not_finalized(session, employee_id=employee_id, months=leave_months)`
   - `api/leaves.py:1131`, `:1438`, `:1785`: same transformation
4. Replace `_collect_leave_months(start, end)` calls with `collect_months_from_range(start, end)`.

- [ ] **Step 2.6: Update `api/overtimes.py` — 接上 single-day check**

Find the function that handles overtime create/update/approve. Where it currently has manual single-month check (look around `api/overtimes.py:1234` near `lock_and_premark_stale(session, employee_id, {overtime_month})`), add:

```python
from services.salary.finalize_guard import (
    collect_months_from_dates,
    assert_months_not_finalized,
)

# Before the lock call:
assert_months_not_finalized(
    session, employee_id=employee_id, months=collect_months_from_dates([ot.ot_date])
)
```

Apply to all overtime CUD endpoints (search for `lock_and_premark_stale` in overtimes.py — pattern: assert guard before lock).

- [ ] **Step 2.7: Update `api/punch_corrections.py` — 接上 single-day check**

Similar pattern: before `lock_and_premark_stale`, add:

```python
from services.salary.finalize_guard import (
    collect_months_from_dates,
    assert_months_not_finalized,
)

assert_months_not_finalized(
    session, employee_id=pc.employee_id,
    months=collect_months_from_dates([pc.target_date]),
)
```

- [ ] **Step 2.8: Run full suite**

```bash
pytest tests/test_finalize_guard.py tests/test_leaves*.py tests/test_overtime*.py tests/test_punch*.py -v
pytest -x  # 全測試
```
Expected: PASS, no regression.

- [ ] **Step 2.9: Commit**

```bash
git add services/salary/finalize_guard.py api/leaves.py api/overtimes.py api/punch_corrections.py tests/test_finalize_guard.py tests/conftest.py
git commit -m "$(cat <<'EOF'
refactor(salary): 抽 finalize_guard 統一封存守衛

leaves 既有 _check_salary_months_not_finalized + _collect_leave_months 抽到 services/salary/finalize_guard.py，
推廣為 collect_months_from_range / collect_months_from_dates；overtimes/punch_corrections 接上 single-day check。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Commit 3 — `lock_and_premark_stale` context manager 包裝

**Goal:** 包出 `lock_and_mark_stale` context manager 形式（保留原 `lock_and_premark_stale` 函式相容）。**實作者評估**：若認為無資源清理需求、context manager 反而增加閱讀成本，可直接 skip 此 commit 並回報 spec 修訂。

**Files:**
- Modify: `services/salary/utils.py:214` (新增 context manager wrapper)
- Create: `tests/test_stale_marker_context.py`

- [ ] **Step 3.1: Write failing test**

Create `tests/test_stale_marker_context.py`:

```python
"""驗證 lock_and_mark_stale context manager：正常 exit + 例外時 advisory lock 隨 commit/rollback 釋放。"""
import pytest
from services.salary.utils import lock_and_mark_stale


def test_lock_and_mark_stale_context_marks_stale_on_enter(
    db_session, salary_record_factory
):
    rec = salary_record_factory(employee_id=1, year=2026, month=5, is_finalized=False)
    with lock_and_mark_stale(db_session, employee_id=1, months={(2026, 5)}):
        db_session.flush()
        # within ctx，rec 應已標 stale
        db_session.refresh(rec)
        assert rec.needs_recalc is True


def test_lock_and_mark_stale_context_skips_finalized(
    db_session, finalized_salary_record_factory
):
    rec = finalized_salary_record_factory(employee_id=1, year=2026, month=5)
    with lock_and_mark_stale(db_session, employee_id=1, months={(2026, 5)}):
        pass
    # finalized 月份不應被標 stale
    db_session.refresh(rec)
    assert getattr(rec, "needs_recalc", False) is False


def test_lock_and_mark_stale_context_rollback_releases_via_session(db_session):
    """advisory lock 在 commit/rollback 時釋放，context exit 不額外做事"""
    try:
        with lock_and_mark_stale(db_session, employee_id=1, months={(2026, 7)}):
            raise RuntimeError("simulated")
    except RuntimeError:
        db_session.rollback()
    # No assertion needed: 確認沒有 deadlock 即可（test 跑完不 hang）
```

- [ ] **Step 3.2: Run — expect FAIL** (`lock_and_mark_stale` not exported)

```bash
pytest tests/test_stale_marker_context.py -v
```

- [ ] **Step 3.3: Add context manager to `services/salary/utils.py`**

Append at end of `services/salary/utils.py`:

```python
from contextlib import contextmanager


@contextmanager
def lock_and_mark_stale(
    session, *, employee_id: int, months: set[tuple[int, int]]
):
    """`lock_and_premark_stale` 的 context manager 包裝。

    Why context form: 讓 caller 寫成 `with ...:` 區塊強調「鎖窗範圍 = block 內容」，
    減少漏 commit 或 mark_stale 在多分支邏輯後寫漏的風險。
    advisory lock 由 session 的 commit/rollback 自動釋放，context exit 不額外做事。
    """
    lock_and_premark_stale(session, employee_id, months)
    yield
```

- [ ] **Step 3.4: Run tests — expect PASS**

```bash
pytest tests/test_stale_marker_context.py -v
```

- [ ] **Step 3.5: Migrate 三 router 的呼叫點到 context form（漸進）**

**只改最容易出錯的點**（多分支函式），其他維持函式呼叫即可：
- `api/leaves.py:1000`, `:1134`, `:1317`, `:1848`（4 處 leave 主流程）
- `api/overtimes.py:1084`, `:1234`, `:1351`, `:1601`（4 處 ot 主流程）
- `api/punch_corrections.py`（搜 `lock_and_premark_stale` 全部換為 `with lock_and_mark_stale(...):`）

Pattern：
```python
# Before
lock_and_premark_stale(session, emp_id, months)
# ... mutation logic
session.commit()

# After
with lock_and_mark_stale(session, employee_id=emp_id, months=months):
    # ... mutation logic
    session.commit()
```

- [ ] **Step 3.6: Run full suite**

```bash
pytest -x
```

- [ ] **Step 3.7: Commit**

```bash
git add services/salary/utils.py api/leaves.py api/overtimes.py api/punch_corrections.py tests/test_stale_marker_context.py
git commit -m "$(cat <<'EOF'
refactor(salary): lock_and_premark_stale 加 context manager 包裝

新增 lock_and_mark_stale ctxmgr，三 router 主流程改 with-block 形式，
原 lock_and_premark_stale 函式保留相容。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Commit 4 — `services/notification/approval_notifier.py`

**Goal:** 抽 LINE 通知統一入口 `notify_approval(doc_type, action, ...)`，內部 dispatch 到 `LineService.notify_leave_result` / `notify_overtime_result` / 新增的 `notify_punch_correction_result`。三 router 改用此入口。

**Files:**
- Create: `services/notification/__init__.py`（空檔）
- Create: `services/notification/approval_notifier.py`
- Modify: `services/line_service.py` — 新增 `notify_punch_correction_result` 方法
- Modify: `api/leaves.py:1540-1554`, `:1921`, `:2006-2013` (LINE push 點)
- Modify: `api/overtimes.py:305-320` (LINE push 點)
- Modify: `api/punch_corrections.py` — 補上 LINE 通知（目前沒有）
- Create: `tests/test_approval_notifier.py`

- [ ] **Step 4.1: Write failing test**

```python
"""驗證 notify_approval 統一入口：dispatch 正確、failure log 不拋、reason 帶入駁回。"""
from unittest.mock import MagicMock
from datetime import date

from services.notification.approval_notifier import notify_approval


def test_notify_approval_leave_approved_dispatches_to_leave_result():
    mock_line = MagicMock()
    notify_approval(
        line_service=mock_line,
        doc_type="leave",
        action="approve",
        line_user_id="U123",
        name="王小明",
        context={"leave_type": "事假", "start": date(2026, 5, 15), "end": date(2026, 5, 16)},
    )
    mock_line.notify_leave_result.assert_called_once_with(
        "U123", "王小明", "事假", date(2026, 5, 15), date(2026, 5, 16), True, None,
    )


def test_notify_approval_leave_rejected_passes_reason():
    mock_line = MagicMock()
    notify_approval(
        line_service=mock_line,
        doc_type="leave",
        action="reject",
        line_user_id="U123",
        name="王小明",
        context={"leave_type": "事假", "start": date(2026, 5, 15), "end": date(2026, 5, 16)},
        rejection_reason="證明不足",
    )
    mock_line.notify_leave_result.assert_called_once_with(
        "U123", "王小明", "事假", date(2026, 5, 15), date(2026, 5, 16), False, "證明不足",
    )


def test_notify_approval_overtime_dispatches_to_overtime_result():
    mock_line = MagicMock()
    notify_approval(
        line_service=mock_line,
        doc_type="overtime",
        action="approve",
        line_user_id="U123",
        name="李小華",
        context={"ot_date": date(2026, 5, 10), "ot_type": "平日"},
    )
    mock_line.notify_overtime_result.assert_called_once_with(
        "U123", "李小華", date(2026, 5, 10), "平日", True,
    )


def test_notify_approval_no_line_service_is_noop():
    """line_service=None 時靜默跳過（dev 環境 LIFF 未設）"""
    notify_approval(
        line_service=None,
        doc_type="leave",
        action="approve",
        line_user_id="U123",
        name="王小明",
        context={"leave_type": "事假", "start": date(2026, 5, 15), "end": date(2026, 5, 15)},
    )
    # 不拋例外即可


def test_notify_approval_line_service_failure_is_swallowed():
    """LineService 內部失敗（API down）應 log 不拋"""
    mock_line = MagicMock()
    mock_line.notify_leave_result.side_effect = RuntimeError("LINE API down")
    notify_approval(
        line_service=mock_line,
        doc_type="leave",
        action="approve",
        line_user_id="U123",
        name="王小明",
        context={"leave_type": "事假", "start": date(2026, 5, 15), "end": date(2026, 5, 15)},
    )
    # 不拋例外即可
```

- [ ] **Step 4.2: Run — expect FAIL**

```bash
pytest tests/test_approval_notifier.py -v
```

- [ ] **Step 4.3: Create `services/notification/__init__.py`**

Empty file.

- [ ] **Step 4.4: Create `services/notification/approval_notifier.py`**

```python
"""統一審核結果 LINE 通知入口。

Why centralize: 三 router 原本各自呼叫 LineService.notify_*_result，時序與 reason 帶入不一致。
本入口由 caller 在 commit 後呼叫，內部 dispatch 並 swallow LineService 例外。
"""
import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

DocType = Literal["leave", "overtime", "punch_correction"]
Action = Literal["approve", "reject"]


def notify_approval(
    *,
    line_service: Any | None,
    doc_type: DocType,
    action: Action,
    line_user_id: str | None,
    name: str,
    context: dict,
    rejection_reason: str | None = None,
) -> None:
    """非阻塞通知。caller 必須在 DB commit 後呼叫。"""
    if line_service is None or not line_user_id:
        return
    approved = action == "approve"
    try:
        if doc_type == "leave":
            line_service.notify_leave_result(
                line_user_id, name,
                context["leave_type"], context["start"], context["end"],
                approved, rejection_reason,
            )
        elif doc_type == "overtime":
            line_service.notify_overtime_result(
                line_user_id, name,
                context["ot_date"], context["ot_type"], approved,
            )
        elif doc_type == "punch_correction":
            line_service.notify_punch_correction_result(
                line_user_id, name,
                context["target_date"], approved, rejection_reason,
            )
    except Exception as exc:
        logger.warning(
            "LINE notify_approval 失敗（doc_type=%s action=%s user=%s）：%s",
            doc_type, action, line_user_id, exc,
        )
```

- [ ] **Step 4.5: Add `notify_punch_correction_result` to LineService**

In `services/line_service.py` after `notify_overtime_result` (~line 480), add:

```python
def notify_punch_correction_result(
    self,
    line_user_id: str,
    name: str,
    target_date: date,
    approved: bool,
    reason: Optional[str] = None,
) -> None:
    """補打卡審核結果個人推播（失敗時 log warning，不拋出）"""
    status = "已核准" if approved else "已駁回"
    suffix = f"\n原因：{reason}" if (not approved and reason) else ""
    text = f"【補打卡審核結果】{name} {target_date} 的補打卡{status}{suffix}"
    self._push_to_user(line_user_id, text)
```

- [ ] **Step 4.6: Migrate three router LINE call sites**

Find each existing `_line_service.notify_leave_result(...)` and `notify_overtime_result(...)` call:
- `api/leaves.py:1554`, `:2013` — replace with `notify_approval(line_service=_line_service, doc_type="leave", ...)`
- `api/overtimes.py:320` — replace with `notify_approval(line_service=_line_service, doc_type="overtime", ...)`
- `api/punch_corrections.py` — 在 approve / reject 後新增 `notify_approval(...)` 呼叫（之前沒有）

Add `from services.notification.approval_notifier import notify_approval` to each router's imports.

- [ ] **Step 4.7: Run all tests**

```bash
pytest tests/test_approval_notifier.py -v
pytest -x
```

- [ ] **Step 4.8: Commit**

```bash
git add services/notification services/line_service.py api/leaves.py api/overtimes.py api/punch_corrections.py tests/test_approval_notifier.py
git commit -m "$(cat <<'EOF'
refactor(notification): 抽 approval_notifier 統一審核結果 LINE 入口

新增 services/notification/approval_notifier.notify_approval；三 router 統一呼叫，
LineService 補 notify_punch_correction_result（pc 原本無 LINE 通知）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Stage 1 結束

- [ ] **Stage 1 驗收**

```bash
pytest -x  # 全綠
git log --oneline refactor/attendance-consolidation-stage1 ^main  # 4 commits
```

> **MERGE Stage 1 到 main 後，再 checkout Stage 2 分支：**
> ```bash
> git checkout main && git merge --no-ff refactor/attendance-consolidation-stage1
> git checkout -b refactor/attendance-consolidation-stage2
> ```

---

## Stage 2 — 行為層整合（commits 5-9）

### Task 5: Commit 5 — `services/approval/batch_executor.py`

**Goal:** 抽兩段提交 batch approval helper，三 router 改用。**行為差異**：ot/pc 從「邊驗邊寫」改為 fail-fast（任一失敗全 abort）。前端 `BatchApprovalDialog` 已處理 422 mixed result，但需確認 schema 對齊。

**Files:**
- Create: `services/approval/__init__.py`
- Create: `services/approval/batch_executor.py`
- Modify: `api/leaves.py` batch_approve endpoint
- Modify: `api/overtimes.py` batch_approve endpoint
- Modify: `api/punch_corrections.py` batch_approve endpoint（如有）
- Create: `tests/test_batch_executor.py`

- [ ] **Step 5.1: Write failing test**

```python
"""驗證 batch_executor 兩段提交：fail-fast、權限檢查順序、commit 後通知時序。"""
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from services.approval.batch_executor import execute_batch_approval, BatchResult


def test_batch_approve_all_pass(db_session, leave_factory):
    leaves = [leave_factory() for _ in range(3)]
    side_effect_calls = []

    def validator(s, rec):
        pass  # all valid

    def side_effects(s, recs):
        side_effect_calls.append([r.id for r in recs])

    result = execute_batch_approval(
        session=db_session,
        doc_type="leave",
        record_ids=[lv.id for lv in leaves],
        action="approve",
        actor={"id": 1, "username": "admin", "role": "admin"},
        validator=validator,
        side_effects=side_effects,
        record_loader=lambda s, ids: db_session.query(type(leaves[0])).filter(
            type(leaves[0]).id.in_(ids)
        ).with_for_update().all(),
    )
    assert isinstance(result, BatchResult)
    assert len(result.succeeded) == 3
    assert result.failed == []
    assert len(side_effect_calls) == 1


def test_batch_approve_fail_fast_one_invalid_aborts_all(db_session, leave_factory):
    leaves = [leave_factory() for _ in range(5)]

    def validator(s, rec):
        if rec.id == leaves[2].id:
            raise HTTPException(status_code=422, detail="overlap")

    def side_effects(s, recs):
        raise AssertionError("side_effects should not run when validator fails")

    result = execute_batch_approval(
        session=db_session,
        doc_type="leave",
        record_ids=[lv.id for lv in leaves],
        action="approve",
        actor={"id": 1, "username": "admin", "role": "admin"},
        validator=validator,
        side_effects=side_effects,
        record_loader=lambda s, ids: db_session.query(type(leaves[0])).filter(
            type(leaves[0]).id.in_(ids)
        ).all(),
    )
    assert result.succeeded == []
    assert len(result.failed) == 1
    assert result.failed[0]["id"] == leaves[2].id
    assert result.failed[0]["reason"] == "overlap"
```

> **Note:** `leave_factory` fixture 在 `tests/conftest.py`，已存在或需補。

- [ ] **Step 5.2: Run — expect FAIL**

- [ ] **Step 5.3: Create `services/approval/__init__.py`** (empty)

- [ ] **Step 5.4: Create `services/approval/batch_executor.py`**

```python
"""兩段提交 batch approval executor。

Why fail-fast: leaves 已實作此模式（2026-05-12 修補），overtimes/punch_corrections 原本「邊驗邊寫」
會留下 partial commit state。統一為「全部 validator 通過才進 side_effects + commit」。

Pass 1: record_loader 載入（含 with_for_update）+ validator 全跑
Pass 2: 全過 → 寫 ApprovalLog + 變更狀態 + side_effects + commit
Pass 3: commit 後由 caller 推 LINE（透過 approval_notifier）
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from fastapi import HTTPException

from utils.approval_helpers import _write_approval_log


@dataclass
class BatchResult:
    succeeded: list[Any] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)


def execute_batch_approval(
    *,
    session,
    doc_type: str,
    record_ids: list[int],
    action: Literal["approve", "reject"],
    actor: dict,
    validator: Callable[[Any, Any], None],
    side_effects: Callable[[Any, list], None],
    record_loader: Callable[[Any, list[int]], list],
    rejection_reason: str | None = None,
) -> BatchResult:
    """兩段提交。Pass 1 全 validate，全過才 Pass 2。"""
    records = record_loader(session, record_ids)
    result = BatchResult()

    for rec in records:
        try:
            validator(session, rec)
        except HTTPException as exc:
            result.failed.append({"id": rec.id, "reason": exc.detail})

    if result.failed:
        # fail-fast: 任一失敗即不執行 side_effects
        return result

    # All valid — Pass 2
    for rec in records:
        _write_approval_log(
            session=session,
            doc_type=doc_type,
            doc_id=rec.id,
            action=action,
            approver=actor,
            comment=rejection_reason,
        )
        result.succeeded.append(rec)

    side_effects(session, records)
    session.commit()
    return result
```

- [ ] **Step 5.5: Run tests — expect PASS**

- [ ] **Step 5.6: Migrate `api/leaves.py` batch_approve endpoint**

Find `batch_approve` endpoint in `api/leaves.py`（搜 `def batch_approve` 或 `/batch_approve`）。替換內部「load → validate → 各別 commit」為呼叫 `execute_batch_approval`：

```python
# Pseudo-code，依實際 endpoint 結構調整
def validator(s, leave):
    # 原本檢查：權限、狀態、跨類衝突等
    ...

def side_effects(s, leaves):
    # 原本：mark_stale、call notifier in commit-post phase
    for lv in leaves:
        months = collect_months_from_range(lv.start_date, lv.end_date)
        lock_and_premark_stale(s, lv.employee_id, months)

result = execute_batch_approval(
    session=session, doc_type="leave",
    record_ids=req.ids, action=req.action, actor=current_user_dict,
    validator=validator, side_effects=side_effects,
    record_loader=lambda s, ids: s.query(Leave).filter(Leave.id.in_(ids)).with_for_update().all(),
    rejection_reason=req.rejection_reason,
)

# Post-commit LINE notify
for lv in result.succeeded:
    notify_approval(line_service=_line_service, doc_type="leave", action=req.action, ...)

return {"succeeded": [lv.id for lv in result.succeeded], "failed": result.failed}
```

- [ ] **Step 5.7: Migrate `api/overtimes.py` 與 `api/punch_corrections.py` batch_approve**

同模式。

- [ ] **Step 5.8: Run full suite**

```bash
pytest -x
```

- [ ] **Step 5.9: Commit**

```bash
git add services/approval/__init__.py services/approval/batch_executor.py api/leaves.py api/overtimes.py api/punch_corrections.py tests/test_batch_executor.py
git commit -m "$(cat <<'EOF'
refactor(approval): 抽 batch_executor 兩段提交

leaves/overtimes/punch_corrections batch_approve 統一改 fail-fast：
Pass 1 全 validate，任一失敗即 abort；全過才寫入 + commit + 推 LINE。
BREAKING: ot/pc 原本 partial-success 行為改為全過或全 abort。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Commit 6 — `services/approval/delegate.py`（無行為變更：抽 leave 既有邏輯）

**Goal:** 把 `api/leaves.py` 內代理人解析邏輯抽到 `services/approval/delegate.py`。**無行為變更**。

**Files:**
- Create: `services/approval/delegate.py`
- Modify: `api/leaves.py` — 改 import
- Create: `tests/test_delegate_resolver.py`

- [ ] **Step 6.1: 先在 leaves.py 找到代理人邏輯**

```bash
grep -n "delegate_id\|代理人\|delegate_employee" api/leaves.py | head -30
```

定位現有 helper 或內聯邏輯，記下函式名與行號（行號根據實際結果填入下方）。

- [ ] **Step 6.2: Write failing test**

Create `tests/test_delegate_resolver.py`:

```python
"""驗證 resolve_delegate_for_leave：班導 > 副班導 > 同 classroom 任一。"""
import pytest
from services.approval.delegate import resolve_delegate_for_leave


def test_resolve_delegate_prefers_homeroom_teacher(
    db_session, leave_factory, employee_factory, classroom_factory
):
    cls = classroom_factory()
    homeroom = employee_factory(role="homeroom", classroom=cls)
    assistant = employee_factory(role="assistant", classroom=cls)
    requester = employee_factory(role="teacher", classroom=cls)
    lv = leave_factory(employee=requester)

    delegate = resolve_delegate_for_leave(db_session, lv)
    assert delegate.id == homeroom.id


def test_resolve_delegate_falls_back_to_assistant_when_no_homeroom(
    db_session, leave_factory, employee_factory, classroom_factory
):
    cls = classroom_factory()
    assistant = employee_factory(role="assistant", classroom=cls)
    requester = employee_factory(role="teacher", classroom=cls)
    lv = leave_factory(employee=requester)

    delegate = resolve_delegate_for_leave(db_session, lv)
    assert delegate.id == assistant.id


def test_resolve_delegate_returns_none_when_solo_in_classroom(
    db_session, leave_factory, employee_factory, classroom_factory
):
    cls = classroom_factory()
    requester = employee_factory(role="homeroom", classroom=cls)
    lv = leave_factory(employee=requester)

    delegate = resolve_delegate_for_leave(db_session, lv)
    assert delegate is None
```

- [ ] **Step 6.3: Run — expect FAIL**

- [ ] **Step 6.4: Create `services/approval/delegate.py`**

```python
"""代理人解析：班導 > 副班導 > 同 classroom 任一。

從 api/leaves.py 抽出。無行為變更。
"""
from models.database import Employee


def resolve_delegate_for_leave(session, leave) -> Employee | None:
    """為 leave 申請選擇代理人。回傳 None 表示無合適代理。"""
    # NOTE: implementer — 把 api/leaves.py 內現有邏輯搬過來，照樣 import session.query(Employee) 等
    # 優先序：role='homeroom' > role='assistant' > 同 classroom 其他任一（排除 requester 自己）
    raise NotImplementedError("從 api/leaves.py 抽過來")
```

- [ ] **Step 6.5: 從 `api/leaves.py` 搬實作過來**

把 step 6.1 找到的代理人邏輯整段搬到 `services/approval/delegate.py` 的函式內。`api/leaves.py` 原處改為 `from services.approval.delegate import resolve_delegate_for_leave` + 呼叫。

- [ ] **Step 6.6: Run tests — expect PASS**

- [ ] **Step 6.7: Commit**

```bash
git add services/approval/delegate.py api/leaves.py tests/test_delegate_resolver.py
git commit -m "$(cat <<'EOF'
refactor(approval): 抽 resolve_delegate_for_leave 代理人解析

從 api/leaves.py 搬到 services/approval/delegate.py，無行為變更。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Commit 7 — `resolve_cross_type_offset` 新增 + feature flag + release notes

**Goal:** 新增 leave↔OT 跨類抵扣（單向：approve leave 時偵測同員工同日已核准未發放 OT，回傳要 offset 的 OT）。**金流影響高**，加環境變數 feature flag `ENABLE_LEAVE_OT_OFFSET` 預設 false。

**Files:**
- Modify: `services/approval/delegate.py` — 新增 `resolve_cross_type_offset`
- Modify: `api/leaves.py` — 在 approve leave flow 加入 offset hook（feature flag gated）
- Modify: `models/database.py` 或 `models/overtime.py` — 若需新增 `Overtime.offset_by_leave_id` 欄位（待確認；若 leave/ot 雙向引用較重，可用 ApprovalLog metadata 紀錄即可）
- Create: `tests/test_cross_type_offset.py`
- Create or append: `RELEASE_NOTES.md`

> **⚠️ 實作前 decision point:**
> 走「ApprovalLog metadata 記錄」（簡單，無 schema 異動）or 「Overtime 加 offset_by_leave_id FK」（清晰，需 migration）。
> Plan 預設 **metadata 路徑**。實作者若評估 audit 查詢頻繁需要 FK，可改 schema 路徑（plan 註記，避免擅自）。

- [ ] **Step 7.1: Write failing tests**

```python
"""驗證 cross_type_offset：OT 足/不足/已部分領取/feature flag 關閉時 noop。"""
import os
from unittest.mock import patch

import pytest

from services.approval.delegate import resolve_cross_type_offset


def test_offset_returns_matching_ot_when_flag_on(
    db_session, leave_factory, overtime_factory, employee_factory
):
    with patch.dict(os.environ, {"ENABLE_LEAVE_OT_OFFSET": "true"}):
        emp = employee_factory()
        lv = leave_factory(employee=emp, date="2026-05-15")
        ot = overtime_factory(employee=emp, ot_date="2026-05-15", is_approved=True, paid=False)
        result = resolve_cross_type_offset(db_session, lv)
        assert result is not None
        assert result.id == ot.id


def test_offset_returns_none_when_flag_off(
    db_session, leave_factory, overtime_factory, employee_factory
):
    with patch.dict(os.environ, {"ENABLE_LEAVE_OT_OFFSET": "false"}):
        emp = employee_factory()
        lv = leave_factory(employee=emp, date="2026-05-15")
        overtime_factory(employee=emp, ot_date="2026-05-15", is_approved=True, paid=False)
        result = resolve_cross_type_offset(db_session, lv)
        assert result is None


def test_offset_skips_already_paid_ot(
    db_session, leave_factory, overtime_factory, employee_factory
):
    with patch.dict(os.environ, {"ENABLE_LEAVE_OT_OFFSET": "true"}):
        emp = employee_factory()
        lv = leave_factory(employee=emp, date="2026-05-15")
        overtime_factory(employee=emp, ot_date="2026-05-15", is_approved=True, paid=True)
        result = resolve_cross_type_offset(db_session, lv)
        assert result is None


def test_offset_skips_unapproved_ot(
    db_session, leave_factory, overtime_factory, employee_factory
):
    with patch.dict(os.environ, {"ENABLE_LEAVE_OT_OFFSET": "true"}):
        emp = employee_factory()
        lv = leave_factory(employee=emp, date="2026-05-15")
        overtime_factory(employee=emp, ot_date="2026-05-15", is_approved=False)
        result = resolve_cross_type_offset(db_session, lv)
        assert result is None
```

- [ ] **Step 7.2: Run — expect FAIL**

- [ ] **Step 7.3: Append to `services/approval/delegate.py`**

```python
import os
from datetime import date as _date

from models.database import Overtime


def resolve_cross_type_offset(session, leave) -> Overtime | None:
    """approve leave 時偵測同員工同日已核准但未發放的 OT，回傳要 offset 的 OT 紀錄。

    Feature flag `ENABLE_LEAVE_OT_OFFSET`（環境變數，預設 false）。
    單向觸發：僅在 approve leave 流程呼叫，approve OT 時不反向觸發。
    """
    if os.environ.get("ENABLE_LEAVE_OT_OFFSET", "").lower() not in ("1", "true", "yes"):
        return None
    # leave 為跨月時，offset 範圍以 leave.start_date 開始當日為基準（業務語意：當日彼此抵扣）
    # 多日 leave 時，這版只處理第一日 — 待業主確認後續延伸
    target_dates: list[_date] = []
    if hasattr(leave, "leave_date") and leave.leave_date:
        target_dates = [leave.leave_date]
    elif hasattr(leave, "start_date") and leave.start_date:
        target_dates = [leave.start_date]
    if not target_dates:
        return None
    return (
        session.query(Overtime)
        .filter(
            Overtime.employee_id == leave.employee_id,
            Overtime.ot_date.in_(target_dates),
            Overtime.is_approved == True,
            Overtime.paid == False,  # 假設 Overtime 有 paid 欄位；如名稱不同請改
        )
        .first()
    )
```

> **NOTE for implementer:** 確認 `Overtime` model 實際欄位名（`paid` / `is_paid` / `disbursed` 等），跑前先看 `models/database.py` 或 `models/overtime.py`。

- [ ] **Step 7.4: Run tests — expect PASS**

- [ ] **Step 7.5: 在 `api/leaves.py` approve flow 接上**

Find leave approve endpoint（搜 `def approve_leave` 或 `:1491` 附近）。在 commit 前加：

```python
from services.approval.delegate import resolve_cross_type_offset

# Inside approve flow, after validation, before commit
offset_ot = resolve_cross_type_offset(session, leave)
if offset_ot:
    offset_ot.offset_by_leave_id = leave.id  # 若有此欄位
    offset_ot.paid = True  # 或標記為「已抵扣」狀態，依 model 而定
    _write_approval_log(
        session=session,
        doc_type="overtime",
        doc_id=offset_ot.id,
        action="update",
        approver=current_user_dict,
        comment="leave 跨類抵扣",
        metadata={"offset_by_leave_id": leave.id},
    )
```

> **Implementer note:** 確認 schema 後決定欄位名與更新方式；若 Overtime 無 `offset_by_leave_id` 欄位，只在 metadata 紀錄即可。

- [ ] **Step 7.6: Create or update `RELEASE_NOTES.md`**

Append section:

```markdown
## 2026-05-13 leave↔OT 跨類抵扣（feature flag）

新增 `ENABLE_LEAVE_OT_OFFSET` 環境變數（預設 false）。啟用後，approve leave 時若同員工同日有
已核准但未發放的 OT，會自動將該 OT 標為已抵扣，避免「請假當日同時領加班費」。

**影響：** 啟用後加班費總額可能下降。**業務測試：** 啟用前請以 dev DB 跑一輪 115.04 對齊驗證。

**回滾：** 將 `ENABLE_LEAVE_OT_OFFSET` 設為 false（無 DB rollback 需求；已抵扣的 OT 仍保留標記）。
```

- [ ] **Step 7.7: Run full suite (flag off + flag on 兩遍)**

```bash
ENABLE_LEAVE_OT_OFFSET=false pytest -x
ENABLE_LEAVE_OT_OFFSET=true pytest tests/test_cross_type_offset.py tests/test_leaves*.py -x
```

- [ ] **Step 7.8: Commit**

```bash
git add services/approval/delegate.py api/leaves.py RELEASE_NOTES.md tests/test_cross_type_offset.py
git commit -m "$(cat <<'EOF'
feat(approval): leave↔OT 跨類抵扣（feature flag）

approve leave 時自動偵測同日已核准未發放 OT 並標為已抵扣。
ENABLE_LEAVE_OT_OFFSET 環境變數 gated，預設 false。
影響加班費總額，啟用前需業務驗證。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Commit 8 — `utils/excel_io.py` + leaves 匯入接上

**Goal:** 抽 Excel 匯入解析骨架，先用 leaves 作為樣板接上，ot/pc 留 TODO 給候選 #2。

**Files:**
- Create: `utils/excel_io.py`
- Modify: `api/leaves.py` 或對應 leave Excel 匯入端點（搜 `import_leaves_from_excel` 或類似）
- Create: `tests/test_excel_io.py`

- [ ] **Step 8.1: 先看現有 leaves 匯入實作**

```bash
grep -n "openpyxl\|load_workbook\|import.*excel\|upload.*excel" api/leaves.py | head
```

定位現有匯入函式，紀錄欄位/驗證邏輯。

- [ ] **Step 8.2: Write failing test**

```python
"""驗證 excel_io 骨架：缺欄、型別錯、business validator 失敗的錯誤格式。"""
import io

import pytest
from openpyxl import Workbook

from utils.excel_io import parse_excel, ExcelImportSchema, ImportError


class _LeaveImportSchema(ExcelImportSchema):
    employee_name: str
    start_date: str
    end_date: str
    leave_type: str


def _build_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def test_parse_excel_success():
    xlsx = _build_xlsx([
        ["employee_name", "start_date", "end_date", "leave_type"],
        ["王小明", "2026-05-15", "2026-05-15", "事假"],
    ])
    result = parse_excel(xlsx, schema=_LeaveImportSchema)
    assert result.errors == []
    assert len(result.rows) == 1
    assert result.rows[0].employee_name == "王小明"


def test_parse_excel_missing_column():
    xlsx = _build_xlsx([
        ["employee_name", "start_date", "leave_type"],  # 缺 end_date
        ["王小明", "2026-05-15", "事假"],
    ])
    result = parse_excel(xlsx, schema=_LeaveImportSchema)
    assert any(e["error_code"] == "MISSING_COLUMN" for e in result.errors)


def test_parse_excel_row_validation_error_format():
    xlsx = _build_xlsx([
        ["employee_name", "start_date", "end_date", "leave_type"],
        ["", "2026-05-15", "2026-05-15", "事假"],  # 空 employee_name
    ])
    result = parse_excel(xlsx, schema=_LeaveImportSchema)
    assert len(result.errors) > 0
    err = result.errors[0]
    assert "row" in err and "col" in err and "error_code" in err and "message" in err
```

- [ ] **Step 8.3: Run — expect FAIL**

- [ ] **Step 8.4: Create `utils/excel_io.py`**

```python
"""Excel 匯入骨架 — 統一錯誤格式 {row, col, value, error_code, message}。

Why centralize: leaves/overtimes/shifts 等各自寫 openpyxl 解析，欄位驗證與錯誤回報格式不一致。
本骨架提供 schema 宣告（pydantic-like）+ 統一 ImportResult。
"""
from dataclasses import dataclass, field
from typing import IO, Any

from openpyxl import load_workbook
from pydantic import BaseModel, ValidationError


class ExcelImportSchema(BaseModel):
    """子類別宣告 columns。"""
    class Config:
        extra = "forbid"


class ImportError(Exception):
    pass


@dataclass
class ImportResult:
    rows: list[Any] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def parse_excel(file_or_buffer: IO[bytes], *, schema: type[ExcelImportSchema]) -> ImportResult:
    """解析 Excel，每列轉為 schema instance；錯誤統一格式回 errors list。"""
    result = ImportResult()
    try:
        wb = load_workbook(file_or_buffer, read_only=True, data_only=True)
    except Exception as exc:
        result.errors.append({
            "row": 0, "col": None, "value": None,
            "error_code": "INVALID_FILE", "message": f"無法讀取 Excel：{exc}",
        })
        return result

    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = [str(c) if c is not None else "" for c in next(rows_iter)]
    except StopIteration:
        result.errors.append({
            "row": 0, "col": None, "value": None,
            "error_code": "EMPTY_FILE", "message": "Excel 為空",
        })
        return result

    expected_cols = set(schema.model_fields.keys())
    actual_cols = set(header)
    missing = expected_cols - actual_cols
    if missing:
        result.errors.append({
            "row": 1, "col": None, "value": None,
            "error_code": "MISSING_COLUMN",
            "message": f"缺欄位：{', '.join(sorted(missing))}",
        })
        return result

    for row_idx, raw in enumerate(rows_iter, start=2):
        record = {header[i]: raw[i] for i in range(min(len(header), len(raw)))}
        try:
            result.rows.append(schema(**record))
        except ValidationError as exc:
            for err in exc.errors():
                field_name = err["loc"][0] if err["loc"] else None
                result.errors.append({
                    "row": row_idx,
                    "col": field_name,
                    "value": record.get(field_name) if field_name else None,
                    "error_code": err["type"].upper(),
                    "message": err["msg"],
                })
    return result
```

- [ ] **Step 8.5: Run tests — expect PASS**

- [ ] **Step 8.6: Migrate leaves Excel import endpoint**

In `api/leaves.py`，找到現有 import endpoint。改用 `parse_excel`：

```python
from utils.excel_io import parse_excel, ExcelImportSchema


class LeaveImportRow(ExcelImportSchema):
    employee_name: str
    start_date: str  # 之後 parse 為 date
    end_date: str
    leave_type: str
    reason: str | None = None


@router.post("/leaves/import")
async def import_leaves(file: UploadFile, ...):
    contents = await file.read()
    result = parse_excel(io.BytesIO(contents), schema=LeaveImportRow)
    if result.errors:
        return {"errors": result.errors, "imported": 0}
    # ... 後續 business validator + DB insert ...
```

ot/pc 暫不接（在 plan 中標 TODO）。

- [ ] **Step 8.7: Run full suite**

```bash
pytest -x
```

- [ ] **Step 8.8: Commit**

```bash
git add utils/excel_io.py api/leaves.py tests/test_excel_io.py
git commit -m "$(cat <<'EOF'
refactor(excel): 抽 utils/excel_io 統一匯入解析骨架

leaves Excel 匯入改用 parse_excel + ExcelImportSchema，
錯誤格式統一為 {row, col, value, error_code, message}。
ot/pc 留 TODO（候選 #2 收尾）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Commit 9 — Sweep deprecated 標記與舊註解

**Goal:** 清理 Stage 1/2 過程中沒順手刪的 deprecated 註解、未使用 import、舊 helper（若已無 caller）。

- [ ] **Step 9.1: 全域搜尋 deprecated 標記**

```bash
grep -rn "# DEPRECATED\|# TODO.*candidate" api/ services/ utils/ | head
```

- [ ] **Step 9.2: 確認哪些舊 helper 已無 caller**

For each removed helper（例如若 `_check_salary_months_not_finalized` 還留在 leaves.py top）：

```bash
grep -rn "_check_salary_months_not_finalized" --include="*.py"
```

若僅在 tests 與 dead code 出現，刪除。

- [ ] **Step 9.3: Run full suite + lint**

```bash
pytest -x
ruff check api/ services/ utils/  # 若有 ruff
```

- [ ] **Step 9.4: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore(cleanup): 清除 attendance consolidation deprecated 標記與遺留 helper

Stage 1/2 完成後 sweep：刪除 leaves.py 內已遷移的 helper 殘留、未使用 import。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Stage 2 結束

- [ ] **Stage 2 驗收**

```bash
pytest -x  # 全綠
ENABLE_LEAVE_OT_OFFSET=true pytest tests/test_cross_type_offset.py tests/test_leaves*.py -x
git log --oneline refactor/attendance-consolidation-stage2 ^refactor/attendance-consolidation-stage1
# 應顯示 commits 5-9
```

- [ ] **手動驗證**：本地 `start.sh` 啟動兩端，跑一次 leave 申請→approve→LINE 通知 + batch_approve 5 筆其中 1 筆衝突 → 驗證全部 rollback。

---

## Spec Coverage Self-Review

| Spec 章節 | 對應 Task |
|---|---|
| §1 動機 | （無對應 task，文檔） |
| §2 範圍策略 / A 方案 | 整體 plan 結構 |
| §3 目錄佈局 | Tasks 1-9（檔案路徑） |
| §4 Stage 1 (a)(b)(d)(f) | Tasks 1, 2, 3, 4 |
| §5 Stage 2 (c)(e)(g) | Tasks 5, 6, 7, 8 |
| §6 Commit 切分 | 每 Task 對應一個 commit |
| §7 測試策略 | Tasks 1-8 step "Write failing test" |
| §8 風險與回滾 | Stage 1 / Stage 2 分支策略 + commit 7 feature flag |
| §9 不在範圍內 | （無對應 task） |

**已知 plan-spec 落差（已在 plan 內標明）：**
1. (d) context manager 升級 — plan 註記實作者可評估是否真的需要、若無資源清理需求可保留現狀
2. (e) cross_type_offset metadata path vs FK path — plan 預設 metadata，提供 implementer decision note
3. (e) 多日 leave 只處理第一日 offset — plan 註記待業主確認後續延伸
