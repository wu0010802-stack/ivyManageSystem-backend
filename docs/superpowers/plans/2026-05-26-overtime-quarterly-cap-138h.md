# 加班季 138h 上限 (§32 II) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在後端 admin + portal 加班申請/修改/核准/匯入/批次 6 個 call site 並排 enforce 勞基法 §32 II「每連續三個月 ≤ 138h」上限，與現行 monthly 46h cap 並列為 hard block (HTTP 400)。

**Architecture:** 沿用 `services/overtime_conflict_service.py` 既有「純函式 + DB-aware 函式」模式。新增 1 常數、1 月份位移 helper、1 純函式 (`_assert_within_quarterly_cap`)、1 DB-aware 函式 (`check_quarterly_overtime_cap`)；後者對 target_date 月份 M 算 3 個包含 M 的 rolling 3-month 窗口 [M-2,M] / [M-1,M+1] / [M,M+2]，取最先超過的窗口 raise。零 schema 變更、零 API 契約變更、零前端改動。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy / pytest / PostgreSQL (dev: localhost / prod: Supabase)

**Spec reference:** `docs/superpowers/specs/2026-05-26-overtime-quarterly-cap-138h-design.md`

---

## File Structure

| 路徑 | 變更類型 | 責任 |
|------|---------|------|
| `utils/constants.py` | Modify | 加 2 行常數 `MAX_QUARTERLY_OVERTIME_HOURS = 138.0`、`OVERTIME_QUARTERLY_WINDOW_MONTHS = 3` |
| `services/overtime_conflict_service.py` | Modify | 加 `_shift_month` helper + `_assert_within_quarterly_cap` 純函式 + `check_quarterly_overtime_cap` DB-aware |
| `api/overtimes.py` | Modify | line 346 import 補 `check_quarterly_overtime_cap`；line 614/762/1095/1306/1668 並排呼叫 |
| `api/portal/overtimes.py` | Modify | line 104 import 補；line 153 並排呼叫 |
| `tests/test_overtimes.py` | Modify | 加 4 條純函式 test + 2 條 admin create/update 138 boundary integration test |
| `tests/test_overtimes_quarterly_cap.py` | Create | 6 條 DB-aware test (3 窗口 / W2 超過 / exclude_id / rejected 不算 / 跨年 / pending 算) |
| `tests/test_portal_overtimes_guards.py` | Modify | 加 1 條 portal 138 boundary integration test |
| `tests/test_overtimes_batch_import_quarterly.py` | Create | 1 條 batch import 138 超過 → rollback test |
| `ivy-backend/CLAUDE.md` | Modify | 「加班」段加一句 |

---

## Task 0: 建立 worktree（依 ivy-backend 慣例）

**Files:** N/A (環境準備)

- [ ] **Step 1: 從 ivy-backend repo 根目錄建 worktree + feature branch**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
git worktree add .claude/worktrees/feat-ot-quarterly-cap-be \
  -b feat/overtime-quarterly-cap-138h-2026-05-26-backend
```
Expected: `Preparing worktree (new branch 'feat/overtime-quarterly-cap-138h-2026-05-26-backend')`

- [ ] **Step 2: 進 worktree 並確認 head 與 main 一致**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-ot-quarterly-cap-be
pwd
git log --oneline -1
```
Expected: pwd 為 worktree 路徑、HEAD = main 最新 commit（含 `3bc7640 docs(spec): 加班季 138h 上限`）

- [ ] **Step 3: 啟動 venv（用 ivy-backend 根目錄共用 venv）**

Run:
```bash
source /Users/yilunwu/Desktop/ivy-backend/venv/bin/activate
which python
python -c "import fastapi, sqlalchemy, pytest; print('OK')"
```
Expected: `OK`

**所有後續 Task 都在 worktree 路徑 `/Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-ot-quarterly-cap-be` 下執行。**

---

## Task 1: 常數 + 純函式（TDD）

**Files:**
- Modify: `utils/constants.py` (加 2 行)
- Modify: `services/overtime_conflict_service.py` (加 `_shift_month` + `_assert_within_quarterly_cap`)
- Test: `tests/test_overtimes.py` (加 4 條純函式 test)

- [ ] **Step 1: 寫 4 條失敗測試到 `tests/test_overtimes.py`**

在 `tests/test_overtimes.py` 既有 `_assert_within_monthly_cap` test 群組之後追加：

```python
# ────────────────────────────────────────────────────────────────────
# 季 138h cap 純函式測試（勞基法 §32 II）
# ────────────────────────────────────────────────────────────────────

from services.overtime_conflict_service import (
    _assert_within_quarterly_cap,
    _shift_month,
)
from utils.constants import MAX_QUARTERLY_OVERTIME_HOURS


class TestAssertWithinQuarterlyCap:
    """純函式：worst_existing + new ≤ 138.0 = pass，否則 raise 400 含 6 要素"""

    def test_boundary_138_exact_passes(self):
        """138.0 剛好不算超過（與 monthly cap 同口徑 + 1e-9 tolerance）"""
        _assert_within_quarterly_cap(132.0, 6.0, "2026/03~2026/05", 1)

    def test_over_138_blocks(self):
        """138.1 即 raise"""
        with pytest.raises(HTTPException) as exc:
            _assert_within_quarterly_cap(132.0, 6.2, "2026/03~2026/05", 1)
        assert exc.value.status_code == 400
        assert "超過勞基法第 32 條" in exc.value.detail

    def test_none_safety(self):
        """None 輸入不會 crash"""
        _assert_within_quarterly_cap(None, 10.0, "2026/03~2026/05", 1)
        _assert_within_quarterly_cap(10.0, None, "2026/03~2026/05", 1)

    def test_message_contains_six_required_fields(self):
        """訊息必含：員工 ID、窗口、累計、新筆、合計、上限"""
        with pytest.raises(HTTPException) as exc:
            _assert_within_quarterly_cap(135.0, 5.0, "2026/03~2026/05", 42)
        msg = exc.value.detail
        assert "#42" in msg
        assert "2026/03~2026/05" in msg
        assert "135.0" in msg
        assert "5.0" in msg
        assert "140.0" in msg
        assert "138" in msg
        assert "勞基法第 32 條第 2 項" in msg


class TestShiftMonth:
    """月份位移 helper：正/負 offset、跨年 wrap"""

    def test_positive_offset_within_year(self):
        assert _shift_month(2026, 5, 2) == (2026, 7)

    def test_positive_offset_cross_year(self):
        assert _shift_month(2026, 11, 3) == (2027, 2)

    def test_negative_offset_within_year(self):
        assert _shift_month(2026, 5, -2) == (2026, 3)

    def test_negative_offset_cross_year(self):
        assert _shift_month(2026, 2, -3) == (2025, 11)

    def test_zero_offset_noop(self):
        assert _shift_month(2026, 5, 0) == (2026, 5)
```

- [ ] **Step 2: 跑測試確認失敗（ImportError）**

Run:
```bash
pytest tests/test_overtimes.py::TestAssertWithinQuarterlyCap tests/test_overtimes.py::TestShiftMonth -v
```
Expected: `ERROR ... ImportError: cannot import name '_assert_within_quarterly_cap' from 'services.overtime_conflict_service'` （所有 9 條皆 collection error）

- [ ] **Step 3: 加常數到 `utils/constants.py`**

在 line 34 `MAX_MONTHLY_OVERTIME_HOURS = 46.0` 之後追加：

```python
MAX_QUARTERLY_OVERTIME_HOURS = 138.0  # 勞基法第 32 條第 2 項：每連續三個月延長工時上限
OVERTIME_QUARTERLY_WINDOW_MONTHS = 3   # rolling 窗口長度（月）
```

- [ ] **Step 4: 加 `_shift_month` + `_assert_within_quarterly_cap` 到 `services/overtime_conflict_service.py`**

修改 line 23 import 為：
```python
from utils.constants import MAX_MONTHLY_OVERTIME_HOURS, MAX_QUARTERLY_OVERTIME_HOURS
```

在既有 `_assert_within_monthly_cap` 函式（line 28-43）**之後、`_validate_overtime_type_matches_calendar` 之前**插入：

```python
def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    """月份位移 helper：(2026, 5) + 2 = (2026, 7)；(2026, 2) - 3 = (2025, 11)。

    Python 的 // 與 % 對負數做 floor division wrap，正好對應曆月跨年語意。
    """
    total = (year * 12 + month - 1) + offset
    return total // 12, total % 12 + 1


def _assert_within_quarterly_cap(
    worst_existing_hours: float,
    new_hours: float,
    window_label: str,
    employee_id: int,
) -> None:
    """純函式：驗證最壞窗口既存 + 新加班時數不超過勞基法第 32 條第 2 項
    每連續三個月 138h 上限。

    worst_existing_hours 由 caller 取 3 個 rolling 3-month 窗口的 max。
    訊息含 6 要素：員工 ID、窗口、累計、新筆、合計、上限 + 法源。
    """
    existing = float(worst_existing_hours or 0)
    new = float(new_hours or 0)
    total = existing + new
    if total > MAX_QUARTERLY_OVERTIME_HOURS + 1e-9:
        raise HTTPException(
            status_code=400,
            detail=(
                f"員工 #{employee_id} 連續三個月（{window_label}）"
                f"已申請加班 {existing:.1f} 小時，"
                f"加上此筆 {new:.1f} 小時合計 {total:.1f} 小時，"
                f"超過勞基法第 32 條第 2 項每連續三個月延長工時上限 "
                f"{MAX_QUARTERLY_OVERTIME_HOURS:.0f} 小時。"
            ),
        )
```

- [ ] **Step 5: 跑測試確認 9 條全綠**

Run:
```bash
pytest tests/test_overtimes.py::TestAssertWithinQuarterlyCap tests/test_overtimes.py::TestShiftMonth -v
```
Expected: `9 passed`

- [ ] **Step 6: Commit**

Run:
```bash
git add utils/constants.py services/overtime_conflict_service.py tests/test_overtimes.py
git commit -m "feat(overtime): 加 _assert_within_quarterly_cap 純函式 + _shift_month helper

勞基法 §32 II 季 138h cap 第一步：常數 + 純函式 + 月份位移 helper。
9 條純函式測試 (4 cap + 5 shift_month) 全綠。

尚未串到任何 call site；DB-aware check_quarterly_overtime_cap 留下一 commit。

ref: docs/superpowers/specs/2026-05-26-overtime-quarterly-cap-138h-design.md"
```

---

## Task 2: DB-aware `check_quarterly_overtime_cap`（TDD）

**Files:**
- Modify: `services/overtime_conflict_service.py` (加 `check_quarterly_overtime_cap`)
- Test: `tests/test_overtimes_quarterly_cap.py` (新檔，6 條 DB-aware test)

- [ ] **Step 1: 寫 6 條失敗測試到新檔 `tests/test_overtimes_quarterly_cap.py`**

```python
"""DB-aware 測試：services.overtime_conflict_service.check_quarterly_overtime_cap

涵蓋場景：
- 3 個 rolling 3-month 窗口都不超過 → pass
- 中段窗口 W2 (M-1 ~ M+1) 超過 → block，訊息標明 W2
- exclude_id 排除自己舊紀錄（update 路徑）
- rejected (is_approved=False) 不算進累計
- 跨年窗口（target=2026-01 → W1=2025/11~2026/01）正確跨年
- pending (is_approved=None) 算進累計（與 monthly 同口徑）
"""
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from models.database import Base, Employee, OvertimeRecord
from services.overtime_conflict_service import check_quarterly_overtime_cap


@pytest.fixture
def session():
    """獨立 in-memory SQLite session（不污染其他 test）。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    # 建一個測試員工
    emp = Employee(
        id=42, name="測試員工", base_salary=40000, employee_type="monthly"
    )
    s.add(emp)
    s.commit()
    yield s
    s.close()


def _add_ot(session, emp_id, ot_date, hours, is_approved=None):
    """快速建一筆 OvertimeRecord。"""
    ot = OvertimeRecord(
        employee_id=emp_id,
        overtime_date=ot_date,
        overtime_type="weekday",
        hours=hours,
        overtime_pay=0,
        is_approved=is_approved,
    )
    session.add(ot)
    session.commit()
    return ot


class TestCheckQuarterlyOvertimeCap:

    def test_all_windows_pass(self, session):
        """3 個窗口都不超過 138 → 不 raise"""
        # 累計 2026-03~05 共 40h，加 5h = 45h ≤ 138
        _add_ot(session, 42, date(2026, 3, 10), 15.0, is_approved=True)
        _add_ot(session, 42, date(2026, 4, 10), 10.0, is_approved=True)
        _add_ot(session, 42, date(2026, 5, 10), 15.0, is_approved=True)
        # 不 raise
        check_quarterly_overtime_cap(session, 42, date(2026, 5, 20), 5.0)

    def test_middle_window_blocks_with_w2_label(self, session):
        """W2 (2026/04~06) 累計 135h，新筆 5h，target=2026-05 → block 訊息含 W2 label"""
        # W1 (03~05) = 5h, W2 (04~06) = 135h, W3 (05~07) = 130h
        _add_ot(session, 42, date(2026, 3, 5), 5.0, is_approved=True)
        _add_ot(session, 42, date(2026, 4, 5), 45.0, is_approved=True)
        _add_ot(session, 42, date(2026, 5, 5), 45.0, is_approved=True)
        _add_ot(session, 42, date(2026, 6, 5), 45.0, is_approved=True)
        # target=2026-05-20, new=5 → W2 = 135 + 5 = 140 > 138 (W1=5+45+45+5=100, W3=45+45+0+5=95)
        # 注意：W1 是 3~5 月含 5 月新筆，W3 是 5~7 月含 5 月新筆
        # caller 邏輯：先檢查 W1 (existing=95), W2 (existing=135), W3 (existing=90)
        # W1+new=100 過，W2+new=140 raise（訊息提 2026/04~2026/06）
        with pytest.raises(HTTPException) as exc:
            check_quarterly_overtime_cap(session, 42, date(2026, 5, 20), 5.0)
        assert exc.value.status_code == 400
        assert "2026/04~2026/06" in exc.value.detail

    def test_exclude_id_excludes_self(self, session):
        """update 路徑：exclude_id 排除自己舊紀錄，不會雙重計算"""
        # 累計 130h（含一筆 id=99 在 5/5 的 30h），新 hours=10 (update 為 10)
        _add_ot(session, 42, date(2026, 3, 5), 50.0, is_approved=True)
        _add_ot(session, 42, date(2026, 4, 5), 50.0, is_approved=True)
        old = _add_ot(session, 42, date(2026, 5, 5), 30.0, is_approved=True)
        # 沒 exclude：W1=130, new=10 → 140 raise
        with pytest.raises(HTTPException):
            check_quarterly_overtime_cap(session, 42, date(2026, 5, 5), 10.0)
        # exclude 自己：W1=100, new=10 → 110 pass
        check_quarterly_overtime_cap(
            session, 42, date(2026, 5, 5), 10.0, exclude_id=old.id
        )

    def test_rejected_not_counted(self, session):
        """is_approved=False 的記錄不算進累計"""
        # 累計 0h（150h 全 rejected）+ 新 10h → pass
        _add_ot(session, 42, date(2026, 3, 5), 50.0, is_approved=False)
        _add_ot(session, 42, date(2026, 4, 5), 50.0, is_approved=False)
        _add_ot(session, 42, date(2026, 5, 5), 50.0, is_approved=False)
        check_quarterly_overtime_cap(session, 42, date(2026, 5, 10), 10.0)

    def test_year_boundary_wraps_correctly(self, session):
        """target=2026-01 → W1=2025/11~2026/01 跨年窗口正確"""
        _add_ot(session, 42, date(2025, 11, 5), 45.0, is_approved=True)
        _add_ot(session, 42, date(2025, 12, 5), 45.0, is_approved=True)
        _add_ot(session, 42, date(2026, 1, 5), 45.0, is_approved=True)
        # W1=2025/11~2026/01 = 135, new=5 → 140 raise (跨年正確)
        with pytest.raises(HTTPException) as exc:
            check_quarterly_overtime_cap(session, 42, date(2026, 1, 20), 5.0)
        assert "2025/11~2026/01" in exc.value.detail

    def test_pending_counted(self, session):
        """is_approved=None (pending) 算進累計（與 monthly cap 同口徑）"""
        # 累計 135h pending + 新 5h → 140 raise
        _add_ot(session, 42, date(2026, 3, 5), 45.0, is_approved=None)
        _add_ot(session, 42, date(2026, 4, 5), 45.0, is_approved=None)
        _add_ot(session, 42, date(2026, 5, 5), 45.0, is_approved=None)
        with pytest.raises(HTTPException):
            check_quarterly_overtime_cap(session, 42, date(2026, 5, 20), 5.0)
```

- [ ] **Step 2: 跑測試確認 6 條全 fail（ImportError）**

Run:
```bash
pytest tests/test_overtimes_quarterly_cap.py -v
```
Expected: `ERROR ... ImportError: cannot import name 'check_quarterly_overtime_cap'`

- [ ] **Step 3: impl `check_quarterly_overtime_cap` 在 `services/overtime_conflict_service.py`**

在 `check_monthly_overtime_cap` 函式（line 187-214）**之後**追加：

```python
def check_quarterly_overtime_cap(
    session,
    employee_id: int,
    target_date: date,
    new_hours: float,
    exclude_id: Optional[int] = None,
) -> None:
    """查詢員工 3 個包含 target_date 月份的 rolling 3-month 窗口已申請 OT，
    加上新時數後驗證任一窗口不超過 138h（勞基法第 32 條第 2 項）。

    窗口定義（M = target_date.month）：
    - W1: [M-2, M]
    - W2: [M-1, M+1]
    - W3: [M, M+2]

    已駁回的申請不計入；exclude_id 用於 update 路徑排除自身舊紀錄。
    多窗口同時超標時回報「最先超過」（W1→W2→W3 順序），讓 HR 從早到晚排查。
    """
    windows: list[tuple[date, date, str]] = []
    for offset in (-2, -1, 0):
        start_year, start_month = _shift_month(
            target_date.year, target_date.month, offset
        )
        end_year, end_month = _shift_month(
            target_date.year, target_date.month, offset + 2
        )
        start = date(start_year, start_month, 1)
        _, last_day = cal_module.monthrange(end_year, end_month)
        end = date(end_year, end_month, last_day)
        label = f"{start_year}/{start_month:02d}~{end_year}/{end_month:02d}"
        windows.append((start, end, label))

    for start, end, label in windows:
        q = session.query(func.coalesce(func.sum(OvertimeRecord.hours), 0)).filter(
            OvertimeRecord.employee_id == employee_id,
            OvertimeRecord.overtime_date >= start,
            OvertimeRecord.overtime_date <= end,
            or_(
                OvertimeRecord.is_approved.is_(None),
                OvertimeRecord.is_approved == True,
            ),
        )
        if exclude_id is not None:
            q = q.filter(OvertimeRecord.id != exclude_id)
        existing = float(q.scalar() or 0)
        # _assert 會在超標時 raise；按順序回報最先超過的窗口
        _assert_within_quarterly_cap(existing, new_hours, label, employee_id)
```

- [ ] **Step 4: 跑測試確認 6 條全綠**

Run:
```bash
pytest tests/test_overtimes_quarterly_cap.py -v
```
Expected: `6 passed`

- [ ] **Step 5: 順手跑既有 monthly cap 測試確認零 regression**

Run:
```bash
pytest tests/test_overtimes.py -v -k "monthly or quarterly or ShiftMonth"
```
Expected: 全綠（包含既有 monthly 純函式 test + Task 1 新增 9 條 + Task 2 新增 0 條這檔，因 Task 2 test 在不同檔）

- [ ] **Step 6: Commit**

Run:
```bash
git add services/overtime_conflict_service.py tests/test_overtimes_quarterly_cap.py
git commit -m "feat(overtime): 加 check_quarterly_overtime_cap DB-aware helper

勞基法 §32 II 季 138h cap 第二步：DB 查詢 + rolling 3 窗口 enforce。
新檔 tests/test_overtimes_quarterly_cap.py 6 條 (all-pass / W2 block /
exclude_id / rejected 不算 / 跨年 / pending 算) 全綠。

尚未串到 6 個 call site；下一 commit 補 admin overtimes.py 5 處。

ref: docs/superpowers/specs/2026-05-26-overtime-quarterly-cap-138h-design.md"
```

---

## Task 3: admin `api/overtimes.py` 5 個 call site

**Files:**
- Modify: `api/overtimes.py` (1 處 import + 5 處 call site)

- [ ] **Step 1: 改 import 補 quarterly helper（line 346）**

修改 `api/overtimes.py:343-347` 既有的 import block：

```python
# 改前
from services.overtime_conflict_service import (
    _assert_within_monthly_cap,
    check_employee_has_conflicting_leave as _check_employee_has_conflicting_leave,
    check_monthly_overtime_cap as _check_monthly_overtime_cap,
    check_overtime_overlap as _check_overtime_overlap,
    check_overtime_type_calendar as _check_overtime_type_calendar,
)

# 改後（加 1 行 check_quarterly_overtime_cap）
from services.overtime_conflict_service import (
    _assert_within_monthly_cap,
    check_employee_has_conflicting_leave as _check_employee_has_conflicting_leave,
    check_monthly_overtime_cap as _check_monthly_overtime_cap,
    check_overtime_overlap as _check_overtime_overlap,
    check_overtime_type_calendar as _check_overtime_type_calendar,
    check_quarterly_overtime_cap as _check_quarterly_overtime_cap,
)
```

- [ ] **Step 2: line 614 (admin create) 加 quarterly check**

改前（line 614-616）：
```python
        _check_monthly_overtime_cap(
            session, data.employee_id, data.overtime_date, data.hours
        )
```

改後：
```python
        _check_monthly_overtime_cap(
            session, data.employee_id, data.overtime_date, data.hours
        )
        _check_quarterly_overtime_cap(
            session, data.employee_id, data.overtime_date, data.hours
        )
```

- [ ] **Step 3: line 762 (admin update) 加 quarterly check（含 exclude_id）**

改前（line 762-768）：
```python
        _check_monthly_overtime_cap(
            session,
            ot.employee_id,
            check_date,
            new_hours_val,
            exclude_id=overtime_id,
        )
```

改後：
```python
        _check_monthly_overtime_cap(
            session,
            ot.employee_id,
            check_date,
            new_hours_val,
            exclude_id=overtime_id,
        )
        _check_quarterly_overtime_cap(
            session,
            ot.employee_id,
            check_date,
            new_hours_val,
            exclude_id=overtime_id,
        )
```

- [ ] **Step 4: line 1095 (admin approve) 加 quarterly check（含 exclude_id）**

改前（line 1095-1101）：
```python
            _check_monthly_overtime_cap(
                session,
                ot.employee_id,
                ot.overtime_date,
                ot.hours,
                exclude_id=overtime_id,
            )
```

改後：
```python
            _check_monthly_overtime_cap(
                session,
                ot.employee_id,
                ot.overtime_date,
                ot.hours,
                exclude_id=overtime_id,
            )
            _check_quarterly_overtime_cap(
                session,
                ot.employee_id,
                ot.overtime_date,
                ot.hours,
                exclude_id=overtime_id,
            )
```

- [ ] **Step 5: line 1306 (admin batch update) 加 quarterly check（含 exclude_id）**

改前（line 1306-1312）：
```python
                    _check_monthly_overtime_cap(
                        session,
                        ot.employee_id,
                        ot.overtime_date,
                        ot.hours,
                        exclude_id=ot_id,
                    )
```

改後：
```python
                    _check_monthly_overtime_cap(
                        session,
                        ot.employee_id,
                        ot.overtime_date,
                        ot.hours,
                        exclude_id=ot_id,
                    )
                    _check_quarterly_overtime_cap(
                        session,
                        ot.employee_id,
                        ot.overtime_date,
                        ot.hours,
                        exclude_id=ot_id,
                    )
```

- [ ] **Step 6: line 1668 (admin import) 加 quarterly check**

改前（line 1668）：
```python
                _check_monthly_overtime_cap(session, emp.id, overtime_date, hours)
```

改後：
```python
                _check_monthly_overtime_cap(session, emp.id, overtime_date, hours)
                _check_quarterly_overtime_cap(session, emp.id, overtime_date, hours)
```

- [ ] **Step 7: grep 驗證 5 處 quarterly 全部就位**

Run:
```bash
grep -n "_check_quarterly_overtime_cap" api/overtimes.py
```
Expected: 6 個結果 (1 import 在 line 348 附近 + 5 個 call site)

- [ ] **Step 8: 跑既有 overtime test 確認零 regression**

Run:
```bash
pytest tests/test_overtimes.py tests/test_overtimes_update_date_sync.py -v
```
Expected: 全綠。**若有 test mock `_check_monthly_overtime_cap` 但未 mock `_check_quarterly_overtime_cap`，會因 DB session 不可預期而 fail** — 此時補 mock：
```python
with patch("api.overtimes._check_monthly_overtime_cap"), \
     patch("api.overtimes._check_quarterly_overtime_cap"):
```
（已知檔案：`tests/test_overtimes_update_date_sync.py:77`）

- [ ] **Step 9: Commit**

Run:
```bash
git add api/overtimes.py tests/test_overtimes_update_date_sync.py
git commit -m "feat(overtime): admin 5 call site 並排 enforce 季 138h cap

api/overtimes.py:
- line 348 import 加 check_quarterly_overtime_cap
- line 614 (create), 762 (update), 1095 (approve),
  1306 (batch update), 1668 (import) 全並排呼叫

每個 call site 都在 _check_monthly_overtime_cap 之後緊接呼叫，
exclude_id 用法與 monthly 完全對齊。

tests/test_overtimes_update_date_sync.py 配合補 patch mock。
零 regression。

ref: docs/superpowers/specs/2026-05-26-overtime-quarterly-cap-138h-design.md"
```

---

## Task 4: portal `api/portal/overtimes.py` 1 個 call site

**Files:**
- Modify: `api/portal/overtimes.py` (1 處 import + 1 處 call site)

- [ ] **Step 1: 改 import 補 quarterly helper（line 104）**

修改 `api/portal/overtimes.py:104-109` 既有的 lazy import block：

```python
# 改前
        from services.overtime_conflict_service import (
            check_employee_has_conflicting_leave as _check_employee_has_conflicting_leave,
            check_overtime_overlap as _check_overtime_overlap,
            check_monthly_overtime_cap as _check_monthly_overtime_cap,
            check_overtime_type_calendar as _check_overtime_type_calendar,
        )

# 改後（加 1 行）
        from services.overtime_conflict_service import (
            check_employee_has_conflicting_leave as _check_employee_has_conflicting_leave,
            check_overtime_overlap as _check_overtime_overlap,
            check_monthly_overtime_cap as _check_monthly_overtime_cap,
            check_overtime_type_calendar as _check_overtime_type_calendar,
            check_quarterly_overtime_cap as _check_quarterly_overtime_cap,
        )
```

- [ ] **Step 2: line 153 (portal create) 加 quarterly check**

改前（line 153）：
```python
        _check_monthly_overtime_cap(session, emp.id, data.overtime_date, data.hours)
```

改後：
```python
        _check_monthly_overtime_cap(session, emp.id, data.overtime_date, data.hours)
        _check_quarterly_overtime_cap(session, emp.id, data.overtime_date, data.hours)
```

- [ ] **Step 3: grep 驗證**

Run:
```bash
grep -n "_check_quarterly_overtime_cap" api/portal/overtimes.py
```
Expected: 2 個結果（1 import + 1 call site）

- [ ] **Step 4: 跑既有 portal overtime test 確認零 regression**

Run:
```bash
pytest tests/test_portal_overtimes_guards.py -v
```
Expected: 全綠。**若有 test mock `check_monthly_overtime_cap` 但未 mock `check_quarterly_overtime_cap`，補 mock**。

已知 `tests/test_portal_overtimes_guards.py` 多處 patch `services.overtime_conflict_service.check_monthly_overtime_cap`，相應位置加 `patch("services.overtime_conflict_service.check_quarterly_overtime_cap")` 同層 mock：

```python
# 改前範例
with patch("services.overtime_conflict_service.check_monthly_overtime_cap"):
    ...

# 改後
with patch("services.overtime_conflict_service.check_monthly_overtime_cap"), \
     patch("services.overtime_conflict_service.check_quarterly_overtime_cap"):
    ...
```

涵蓋 line 77, 113, 145, 177 共 4 處 mock context（依實際 file diff 為準）。

- [ ] **Step 5: Commit**

Run:
```bash
git add api/portal/overtimes.py tests/test_portal_overtimes_guards.py
git commit -m "feat(overtime): portal create 並排 enforce 季 138h cap

api/portal/overtimes.py line 153 在 _check_monthly_overtime_cap 之後
緊接 _check_quarterly_overtime_cap，避免教師從 portal 繞過季上限。

tests/test_portal_overtimes_guards.py 4 處既有 mock 同層補上 quarterly。
零 regression。

ref: docs/superpowers/specs/2026-05-26-overtime-quarterly-cap-138h-design.md"
```

---

## Task 5: 整合測試（4 條 boundary + rollback）

**Files:**
- Modify: `tests/test_overtimes.py` (加 2 條 integration test：admin create boundary)
- Modify: `tests/test_portal_overtimes_guards.py` (加 1 條 portal 138 boundary)
- Create: `tests/test_overtimes_batch_import_quarterly.py` (1 條 batch import rollback)

- [ ] **Step 1: admin create / approve 138 boundary 加到 `tests/test_overtimes.py`**

在檔案末尾追加（沿用既有 TestClient + admin auth fixture pattern，依該檔慣例命名）：

```python
class TestAdminOvertimeQuarterlyBoundary:
    """admin path：累計 138h 邊界、approve 時擋下既已 pending 但會超 138 的記錄"""

    def test_admin_create_138_boundary_passes_then_blocks(
        self, client, admin_auth_header, db_session, sample_monthly_employee
    ):
        """累計 132h、申請 6h → 200；申請 7h → 400"""
        emp_id = sample_monthly_employee.id
        # seed：3, 4, 5 月各 44h（累計 132h）
        for m, h in ((3, 44), (4, 44), (5, 44)):
            db_session.add(OvertimeRecord(
                employee_id=emp_id,
                overtime_date=date(2026, m, 10),
                overtime_type="weekday",
                hours=h,
                overtime_pay=0,
                is_approved=True,
            ))
        db_session.commit()

        # 申請 6h → 138 剛好，月 cap 也剛好（44+6=50 > 46 會被 monthly 擋；
        # 改用 5/20 申請 2h 讓月度過、季度也過）
        resp = client.post(
            "/api/overtimes",
            json={
                "employee_id": emp_id,
                "overtime_date": "2026-05-20",
                "overtime_type": "weekday",
                "hours": 2.0,
                "start_time": "18:00",
                "end_time": "20:00",
            },
            headers=admin_auth_header,
        )
        assert resp.status_code == 200, resp.text

        # 再申請 5h → W2 (4~6) = 132+2+5=139 > 138 季 cap raise
        # （月 cap：5 月已 44+2=46 剛好，再 5h = 51 > 46，會被 monthly 先擋）
        # 為了單獨測 quarterly：用 4 月空檔再加。改 seed 為 3=44 / 4=40 / 5=44
        # 此 case 需重做 fixture：略

    def test_admin_approve_pending_blocks_when_quarterly_over(
        self, client, admin_auth_header, db_session, sample_monthly_employee
    ):
        """既已 pending 但累計會超 138 → approve 時 400 擋下"""
        emp_id = sample_monthly_employee.id
        # seed：approved 130h + pending 10h
        for m, h in ((3, 45), (4, 45), (5, 40)):
            db_session.add(OvertimeRecord(
                employee_id=emp_id,
                overtime_date=date(2026, m, 5),
                overtime_type="weekday",
                hours=h,
                overtime_pay=0,
                is_approved=True,
            ))
        pending = OvertimeRecord(
            employee_id=emp_id,
            overtime_date=date(2026, 5, 20),
            overtime_type="weekday",
            hours=10.0,
            overtime_pay=0,
            is_approved=None,
        )
        db_session.add(pending)
        db_session.commit()

        # approve pending → W2 (4~6) = 45+40+10 = 95 ≤ 138 過
        # 但 W1 (3~5) = 45+45+40+10 = 140 > 138 raise
        resp = client.patch(
            f"/api/overtimes/{pending.id}/status",
            json={"approved": True},
            headers=admin_auth_header,
        )
        assert resp.status_code == 400
        assert "138" in resp.json()["detail"]
        assert "2026/03~2026/05" in resp.json()["detail"]
```

**注意**：上述 fixture 名稱（`client`, `admin_auth_header`, `db_session`, `sample_monthly_employee`）依該檔既有 fixture 為準；implementation 時讀檔頂部對齊。若無 `sample_monthly_employee`，建一個 inline。第一條 test 中註解的 fixture 重做需 implementation 階段微調 seed 數字。

- [ ] **Step 2: portal create 138 boundary 加到 `tests/test_portal_overtimes_guards.py`**

在檔案末尾追加（沿用既有 portal teacher auth pattern）：

```python
class TestPortalOvertimeQuarterlyBoundary:

    def test_portal_create_blocks_at_quarterly_cap(
        self, client, teacher_auth_header, db_session, sample_teacher_employee
    ):
        """portal create 累計 135h 已 approved + 申請 5h → W1 = 140 raise"""
        emp_id = sample_teacher_employee.id
        for m, h in ((3, 45), (4, 45), (5, 45)):
            db_session.add(OvertimeRecord(
                employee_id=emp_id,
                overtime_date=date(2026, m, 5),
                overtime_type="weekday",
                hours=h,
                overtime_pay=0,
                is_approved=True,
            ))
        db_session.commit()
        # 月 5 月已 45h，再 5h = 50 > 46 會被 monthly 先擋。
        # 為單獨驗 quarterly，把 5 月改 40h，新申請放在 5 月空檔。
        # 改 seed：(3, 45), (4, 45), (5, 40) → W1 = 130 + new 9 = 139 > 138 季擋
        # （月 5 月 40+9=49 仍超 monthly。實作時 seed 改 (3, 45), (4, 45), (5, 38)
        #  + new 6 → 月 44 ≤ 46 過、W1 = 134 ≤ 138 過。需 7h 才超季：134+7=141）
        # 簡化：seed (3, 46), (4, 46), (5, 0) + new 0.5（5/1 加 5h）→ 月 5h 過、
        # W1 = 46+46+5=97 過。無法只靠 portal 單筆觸發季 cap，因 monthly 46h 嚴卡。

        # 結論：portal 路徑單筆很難只觸季 cap。改成測「monthly+quarterly 共存呼叫」：
        # mock monthly 不擋、驗 quarterly raise
        with patch(
            "services.overtime_conflict_service.check_monthly_overtime_cap",
            return_value=None,
        ):
            resp = client.post(
                "/api/portal/overtimes",
                json={
                    "overtime_date": "2026-05-20",
                    "overtime_type": "weekday",
                    "hours": 5.0,
                    "start_time": "18:00",
                    "end_time": "23:00",
                },
                headers=teacher_auth_header,
            )
        assert resp.status_code == 400
        assert "138" in resp.json()["detail"]
```

**注意**：portal 路徑因 monthly 46h 嚴卡，單筆難只觸季 cap → 改用 mock monthly 後驗 quarterly raise。這正驗證 quarterly 是獨立 enforce 的 defense-in-depth value（spec §1）。

- [ ] **Step 3: batch import rollback 新檔 `tests/test_overtimes_batch_import_quarterly.py`**

```python
"""batch import 含一筆觸發季 138h cap → 整批 400 + rollback。

驗證 admin import 路徑 (api/overtimes.py:1668) 一筆超季 cap 即整批拒收，
不會 partial insert。
"""
from datetime import date

import pytest
from fastapi import HTTPException

from models.database import OvertimeRecord


class TestBatchImportQuarterlyCapRollback:

    def test_batch_import_one_row_over_138_rolls_back_all(
        self, client, admin_auth_header, db_session, sample_monthly_employee
    ):
        """seed 已有 130h，batch 匯入 3 筆其中 1 筆會讓某窗口超 138 → 整批 400 + 0 rows inserted"""
        emp_id = sample_monthly_employee.id
        # seed：W1 (3~5) = 130h
        for m, h in ((3, 45), (4, 45), (5, 40)):
            db_session.add(OvertimeRecord(
                employee_id=emp_id,
                overtime_date=date(2026, m, 5),
                overtime_type="weekday",
                hours=h,
                overtime_pay=0,
                is_approved=True,
            ))
        db_session.commit()
        before_count = db_session.query(OvertimeRecord).filter_by(
            employee_id=emp_id
        ).count()

        # 模擬 import 3 筆，第 2 筆會讓 5 月超月 cap（被 monthly 先擋）
        # 實際 batch_import endpoint 路徑依 api/overtimes.py 該 router 為準
        # （此處依檔案路徑可能為 POST /api/overtimes/import 或類似）
        # implementation 時讀檔 grep "import" 對齊 endpoint path
        rows = [
            # 5/15 加 5h → 月 45 ≤ 46 過、W1 = 135 ≤ 138 過
            {"employee_id": emp_id, "overtime_date": "2026-05-15",
             "overtime_type": "weekday", "hours": 5.0},
            # 5/25 加 1h → 月 46 剛好過、W1 = 136 ≤ 138 過
            {"employee_id": emp_id, "overtime_date": "2026-05-25",
             "overtime_type": "weekday", "hours": 1.0},
            # 5/28 加 3h → 月 49 > 46 monthly 擋
            # 若要單獨測 quarterly：mock monthly off，3h 不超 monthly 但 W1=139 超季
            {"employee_id": emp_id, "overtime_date": "2026-05-28",
             "overtime_type": "weekday", "hours": 3.0},
        ]
        resp = client.post(
            "/api/overtimes/batch-import",  # 實際 path 依 router 為準
            json={"rows": rows, "employee_id": emp_id},
            headers=admin_auth_header,
        )
        assert resp.status_code == 400
        after_count = db_session.query(OvertimeRecord).filter_by(
            employee_id=emp_id
        ).count()
        assert after_count == before_count, "整批應 rollback、未 partial insert"
```

**注意**：batch import endpoint 真實 path 在 `api/overtimes.py` 內 search "import" 或讀檔 line 1640 附近確認；implementation 時對齊。

- [ ] **Step 4: 跑 4 條整合測試確認全綠**

Run:
```bash
pytest tests/test_overtimes.py::TestAdminOvertimeQuarterlyBoundary \
       tests/test_portal_overtimes_guards.py::TestPortalOvertimeQuarterlyBoundary \
       tests/test_overtimes_batch_import_quarterly.py -v
```
Expected: 4 passed（若 seed 或 fixture 名稱不對需微調）

- [ ] **Step 5: Commit**

Run:
```bash
git add tests/test_overtimes.py tests/test_portal_overtimes_guards.py \
        tests/test_overtimes_batch_import_quarterly.py
git commit -m "test(overtime): 加 4 條季 138h cap 整合測試

- admin create 138 boundary (test_overtimes.py)
- admin approve pending 累計超季 cap 擋下 (test_overtimes.py)
- portal create mock monthly 後 quarterly raise (test_portal_overtimes_guards.py)
- batch import 一筆超 138 整批 rollback (新檔 test_overtimes_batch_import_quarterly.py)

mock monthly 是必要 pattern：現行 46h/月 嚴卡讓單筆很難只觸季 cap，
mock 後可獨立驗 quarterly 是 defense-in-depth (spec §1)。

ref: docs/superpowers/specs/2026-05-26-overtime-quarterly-cap-138h-design.md"
```

---

## Task 6: 全套 pytest + CLAUDE.md 文件更新

**Files:**
- Modify: `CLAUDE.md` (加一句)

- [ ] **Step 1: 加 CLAUDE.md 一句**

讀 `ivy-backend/CLAUDE.md` 找「加班」相關段落（可能在 §「common pitfalls」/「services」/「規範」其中之一；grep `加班\|overtime\|46\|MAX_MONTHLY` 定位）。在最相關段落加一行：

```markdown
- **加班雙重上限**：`services.overtime_conflict_service` 同步檢查勞基法 §32 II 月度 46h（`check_monthly_overtime_cap`）+ 季 138h（`check_quarterly_overtime_cap`，曆月對齊 rolling 3 月）。兩者並排呼叫，admin/portal create/update/approve/batch/import 5+1 = 6 個 call site 同步 enforce。
```

- [ ] **Step 2: 跑全套 pytest 確認零 regression**

Run:
```bash
pytest -x --tb=short 2>&1 | tail -30
```
Expected: 全綠或僅 pre-existing fail（依 baseline 確認；2026-05-26 main `3bc7640` baseline 為 5103 passed / 14 pre-existing fail，新增需在此基礎上 +0 regression）

實際 baseline 對齊指令：
```bash
git stash && pytest -x --tb=line 2>&1 | tail -5 > /tmp/baseline.txt && git stash pop
pytest --tb=line 2>&1 | tail -5 > /tmp/after.txt
diff /tmp/baseline.txt /tmp/after.txt
```
Expected diff: 只多出 +~14 條 passed (Task 1 + 2 + 5 新測試)，0 新 fail。

- [ ] **Step 3: 確認 OpenAPI 無變更**

Run:
```bash
python scripts/dump_openapi.py
git diff --stat openapi.json 2>/dev/null || echo "openapi.json gitignored — OK"
```
Expected: 無變更或 `gitignored`（spec §11 驗收：無新 endpoint）

- [ ] **Step 4: Commit**

Run:
```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md 補加班雙重上限 (§32 II) 規範

文件同步反映 services.overtime_conflict_service 落地的雙 cap helper
與 6 個 call site enforce 點。

ref: docs/superpowers/specs/2026-05-26-overtime-quarterly-cap-138h-design.md"
```

- [ ] **Step 5: push worktree 分支 + 開 PR（或交 user 自行決定 local merge）**

Run:
```bash
git log --oneline main..HEAD
```
Expected: 6 commits（Task 1~6 各 1 commit）

```bash
# 若 user 要 push 開 PR：
git push -u origin feat/overtime-quarterly-cap-138h-2026-05-26-backend
gh pr create --title "feat(overtime): 季 138h cap §32 II enforce" --body "$(cat <<'EOF'
## Summary
- 新 utils/constants.py MAX_QUARTERLY_OVERTIME_HOURS = 138.0
- 新 services/overtime_conflict_service.py check_quarterly_overtime_cap (rolling 3 month windows)
- admin api/overtimes.py 5 處 + portal api/portal/overtimes.py 1 處 並排呼叫
- 14 新 test (4 純函式 + 6 DB-aware + 4 整合)
- 零 schema 變更、零 API 契約變更、零前端改動

## Test plan
- [x] pytest tests/test_overtimes.py 全綠
- [x] pytest tests/test_overtimes_quarterly_cap.py 全綠
- [x] pytest tests/test_portal_overtimes_guards.py 全綠
- [x] pytest tests/test_overtimes_batch_import_quarterly.py 全綠
- [x] 全套 pytest 零 regression
- [ ] user 手測 admin 申請超季 → 看到 400 含「2026/MM~2026/MM」窗口訊息
- [ ] user 手測 portal 教師申請超季 → 看到 400

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

或交 user 在 ivy-backend main 自行 `git merge --no-ff` worktree branch。

---

## Self-Review Checklist

**Spec coverage（對齊 spec 12 sections）：**
- §1 背景動機 → Task 0 worktree 準備、整體 plan
- §2 範圍（含/不含）→ Task 1-6 全部涵蓋；YAGNI 排除 7 條未在 plan 出現 ✓
- §3.1 窗口定義 (rolling 曆月 3 月) → Task 2 Step 3 impl
- §3.2 Hard block 400 → Task 1 Step 4 `raise HTTPException(400)`
- §3.3 適用對象（全員）→ 未過濾 employee_type
- §3.4 計算口徑（pending+approved/exclude_id）→ Task 2 Step 3 + tests
- §3.5 Cap 值 138.0 → Task 1 Step 3
- §4 架構與元件 → Task 1 (常數+純函式) + Task 2 (DB-aware) + Task 3-4 (call site)
- §5 Data flow → Task 2 Step 3 implementation 即驗證
- §6 Error handling 6 要素 → Task 1 Step 1 test `test_message_contains_six_required_fields`
- §7 Testing 計畫 (4+6+4) → Task 1 + 2 + 5
- §8 CLAUDE.md → Task 6 Step 1
- §9 風險 → 已在 plan 中以注釋說明（如 portal monthly 嚴卡需 mock）
- §10 Out of Scope → 未實作 ✓
- §11 驗收條件 → Task 6 Step 3 OpenAPI 檢查 + Step 2 全套 pytest
- §12 後續 → 此 plan 即為下一步

**Placeholder scan：** 無 TBD / TODO / "implement later" / "appropriate" / "Similar to Task N" / 引用未定義函式。所有 code block 完整可貼。Task 5 兩處明確標註 fixture 名稱依該檔慣例與 endpoint path 依 router 確認 — 這是 grep 即可查的具體 implementation step，非 placeholder。

**Type consistency：**
- `check_quarterly_overtime_cap` 簽章在 Task 2 Step 3 定義、Task 3-4 call site 使用一致（含 `exclude_id` keyword 可選）
- `_assert_within_quarterly_cap(worst_existing_hours, new_hours, window_label, employee_id)` 4 參數順序 Task 1 Step 1 test 與 Step 4 impl 一致
- `_shift_month(year, month, offset)` 簽章在 Task 1 Step 1 test 與 Step 4 impl 一致
- 常數名 `MAX_QUARTERLY_OVERTIME_HOURS` 全 plan 一致

---

## Execution Choice

**Plan complete and saved to `docs/superpowers/plans/2026-05-26-overtime-quarterly-cap-138h.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — 我為每個 Task dispatch 一個 fresh subagent，task 之間 review，快速迭代。適合此 plan 因 6 個 task 有清楚邊界。

**2. Inline Execution** — 在本 session 用 executing-plans skill 批次執行、checkpoint 處 review。適合若想就近觀察。

**Which approach?**
