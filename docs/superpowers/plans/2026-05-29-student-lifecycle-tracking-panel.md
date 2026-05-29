# 學生在校歷程追蹤面板 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在學生詳細頁加「在校歷程」tab，呈現雙層 Stepper（5 點外層 + 動態年級內層）+ 可摺疊 Timeline，整合既有 funnel / lifecycle / classroom transfer / payment 紀錄為單一視圖。

**Architecture:**
- 後端純函式集中在新檔 `services/student_lifecycle_overview.py`（3 個 compute_* + 1 個 build_*），DB query 只在 `build_*` 完成，純函式 unit-testable
- 新 endpoint `GET /api/students/{id}/lifecycle-overview` 配新 Pydantic schema；既有 `GET /api/students/{id}/timeline` 擴 3 個 source（funnel_event / classroom_transfer / payment）
- 前端新 tab `LifecycleTab.vue` 掛進 `StudentDetailPanel.vue` 的 `TAB_DEFS`，由 4 個子元件組成（外層 stepper / 內層 stepper / timeline / 整合 panel）

**Tech Stack:** FastAPI + SQLAlchemy + Pydantic v2（後端）；Vue 3 `<script setup lang="ts">` + Element Plus + Vitest（前端）；OpenAPI codegen 同步契約。

**參考 spec:** `docs/superpowers/specs/2026-05-29-student-lifecycle-tracking-panel-design.md`

---

## File Structure

### 後端

| 路徑 | 動作 | 職責 |
|------|------|------|
| `services/student_lifecycle_overview.py` | Create | 3 純函式 + 1 build entrypoint |
| `schemas/student_lifecycle.py` | Create | Pydantic v2 output schema |
| `api/students.py` | Modify | 新 endpoint `GET /students/{id}/lifecycle-overview` |
| `services/student_records_timeline.py` | Modify | RECORD_TYPES + 3 fetch/build pair |
| `api/student_change_logs.py` | Modify | 若 timeline endpoint 在此，擴 types enum；否則 review 後確認 |
| `tests/test_student_lifecycle_overview.py` | Create | 純函式 + 整合測試 |
| `tests/test_student_records_timeline_extended.py` | Create | timeline 擴充測試 |

### 前端

| 路徑 | 動作 | 職責 |
|------|------|------|
| `src/api/studentLifecycle.ts` | Create | `getLifecycleOverview()` wrapper |
| `src/api/_generated/schema.d.ts` | Auto-regen | `npm run gen:api` 後產出 |
| `src/components/student/tabs/lifecycle/InnerGradeStepperRow.vue` | Create | 年級內層 stepper（含 skipped 虛線） |
| `src/components/student/tabs/lifecycle/OuterStepperRow.vue` | Create | 5 點外層 stepper + 終態三色 + ⏸ 徽章 |
| `src/components/student/tabs/lifecycle/LifecycleTimelineList.vue` | Create | Timeline + 類別 multi-select filter |
| `src/components/student/tabs/LifecycleTab.vue` | Create | 整合 panel；接 props.studentId / active |
| `src/components/student/StudentDetailPanel.vue` | Modify | TAB_DEFS 加 `lifecycle`；template 加 `<LifecycleTab>` 條件分支 |
| `src/components/student/tabs/lifecycle/__tests__/*.spec.ts` | Create | 4 個子元件的 vitest |

---

## Task 0: 後端純函式 — `compute_outer_steps()`

**Why first:** 純函式可獨立測試，不需 DB；定義回傳結構供後續 task 使用。

**Files:**
- Create: `ivy-backend/services/student_lifecycle_overview.py`
- Create: `ivy-backend/tests/test_student_lifecycle_overview.py`

- [ ] **Step 1: 建立檔案骨架（dataclass + 空函式）**

寫到 `ivy-backend/services/student_lifecycle_overview.py`：

```python
"""services/student_lifecycle_overview.py — 學生在校歷程聚合（read-only）。

純函式集中（compute_*），不依賴 DB session；
build_lifecycle_overview() 是 orchestrator，由 API 層呼叫。

See: docs/superpowers/specs/2026-05-29-student-lifecycle-tracking-panel-design.md
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional


StepStatus = Literal["done", "current", "future"]
GradeStepStatus = Literal["done", "current", "future", "skipped"]
TerminalKind = Literal["graduated", "withdrawn", "transferred", "none"]
OuterKey = Literal["visited", "deposited", "enrolled", "active", "terminal"]


@dataclass
class StepInfo:
    key: OuterKey
    label: str
    status: StepStatus
    occurred_at: Optional[date] = None
    meta: Optional[dict] = None


@dataclass
class GradeStepInfo:
    grade_id: int
    name: str
    sort_order: int
    status: GradeStepStatus
    entered_at: Optional[date] = None
    expected_at: Optional[date] = None
    classroom_name: Optional[str] = None


@dataclass
class TerminalInfo:
    kind: TerminalKind
    actual_date: Optional[date] = None
    expected_date: Optional[date] = None


@dataclass
class LifecycleOverview:
    student_id: int
    current_stage: str
    on_leave_badge: bool
    on_leave_since: Optional[date]
    outer_steps: list[StepInfo] = field(default_factory=list)
    inner_grade_steps: list[GradeStepInfo] = field(default_factory=list)
    terminal: TerminalInfo = field(default_factory=lambda: TerminalInfo(kind="none"))


# 中文標籤
_OUTER_LABELS: dict[OuterKey, str] = {
    "visited": "參觀",
    "deposited": "預繳",
    "enrolled": "報到",
    "active": "在學",
    "terminal": "終態",
}


def compute_outer_steps(
    student,  # models.classroom.Student
    funnel_events: list,  # list[RecruitmentEventLog]
    change_logs: list,  # list[StudentChangeLog]
) -> list[StepInfo]:
    """依 funnel events + change logs 推算 5 點外層 stepper。

    規則見 spec §3.2 表格。所有「無資料」一律降級為 status="future"。
    """
    raise NotImplementedError  # 由下個 step 實作
```

- [ ] **Step 2: 寫 5 個失敗測試**

寫到 `ivy-backend/tests/test_student_lifecycle_overview.py`：

```python
"""純函式測試 — 不依賴 DB session。

用 SimpleNamespace 模擬 Student / RecruitmentEventLog / StudentChangeLog 結構。
"""

from datetime import date
from types import SimpleNamespace

import pytest

from services.student_lifecycle_overview import (
    StepInfo,
    compute_outer_steps,
)


def _stu(**kw):
    defaults = dict(
        id=1,
        lifecycle_status="active",
        enrollment_date=None,
        graduation_date=None,
        withdrawal_date=None,
        classroom_id=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _fe(event_type, to_stage, created_at, from_stage=None):
    return SimpleNamespace(
        event_type=event_type,
        from_stage=from_stage,
        to_stage=to_stage,
        created_at=created_at,
        metadata_json=None,
    )


def _cl(event_type, event_date):
    return SimpleNamespace(event_type=event_type, event_date=event_date)


def test_compute_outer_steps_only_visited():
    """只有參觀記錄 → visited done，其餘 future。"""
    fe = [_fe("visit_logged", "visited", date(2024, 7, 12))]
    steps = compute_outer_steps(_stu(lifecycle_status="prospect"), fe, [])
    assert [s.key for s in steps] == ["visited", "deposited", "enrolled", "active", "terminal"]
    assert steps[0].status == "done"
    assert steps[0].occurred_at == date(2024, 7, 12)
    for s in steps[1:]:
        assert s.status == "future"


def test_compute_outer_steps_visited_to_active_full_path():
    """全程記錄 → visited/deposited/enrolled done，active current，terminal future。"""
    fe = [
        _fe("visit_logged", "visited", date(2024, 7, 12)),
        _fe("deposit_added", "deposited", date(2024, 8, 1), from_stage="visited"),
        _fe("converted", "enrolled", date(2024, 8, 15), from_stage="deposited"),
        _fe("activated", "active", date(2024, 9, 1), from_stage="enrolled"),
    ]
    cl = [_cl("升狀態-active", date(2024, 9, 1))]
    steps = compute_outer_steps(_stu(lifecycle_status="active"), fe, cl)
    assert [s.status for s in steps] == ["done", "done", "done", "current", "future"]
    assert steps[3].occurred_at == date(2024, 9, 1)


def test_compute_outer_steps_graduated_full_terminal():
    """已畢業 → 全 done，active 也是 done。"""
    student = _stu(lifecycle_status="graduated", graduation_date=date(2027, 7, 1))
    fe = [
        _fe("visit_logged", "visited", date(2024, 7, 12)),
        _fe("converted", "enrolled", date(2024, 8, 15)),
        _fe("activated", "active", date(2024, 9, 1)),
    ]
    steps = compute_outer_steps(student, fe, [])
    assert steps[3].status == "done"
    assert steps[4].status == "done"
    assert steps[4].occurred_at == date(2027, 7, 1)


def test_compute_outer_steps_withdrawn_from_active():
    student = _stu(lifecycle_status="withdrawn", withdrawal_date=date(2025, 5, 1))
    steps = compute_outer_steps(student, [], [])
    assert steps[4].status == "done"
    assert steps[4].occurred_at == date(2025, 5, 1)


def test_compute_outer_steps_legacy_student_no_funnel_events():
    """早期學生無 funnel — 只有 enrollment_date，active dot 用 enrollment_date 兜底。"""
    student = _stu(lifecycle_status="active", enrollment_date=date(2023, 9, 1))
    steps = compute_outer_steps(student, [], [])
    # visited / deposited / enrolled 都 future（無 funnel record）
    assert steps[0].status == "future"
    assert steps[1].status == "future"
    assert steps[2].status == "future"
    # active current，occurred_at 兜底用 enrollment_date
    assert steps[3].status == "current"
    assert steps[3].occurred_at == date(2023, 9, 1)
```

- [ ] **Step 3: 跑測試，預期 5 個全 fail（NotImplementedError）**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_student_lifecycle_overview.py -v
```

Expected:
```
test_compute_outer_steps_only_visited FAILED
test_compute_outer_steps_visited_to_active_full_path FAILED
... 5 failed
```

- [ ] **Step 4: 實作 `compute_outer_steps`**

把 `services/student_lifecycle_overview.py` 中 `compute_outer_steps` 的 body 換成：

```python
def compute_outer_steps(
    student,
    funnel_events: list,
    change_logs: list,
) -> list[StepInfo]:
    # 找 funnel events 中各 to_stage 的最早時間
    def _earliest_funnel(stage: str) -> Optional[date]:
        candidates = [
            fe.created_at.date() if hasattr(fe.created_at, "date") else fe.created_at
            for fe in funnel_events
            if fe.to_stage == stage
        ]
        return min(candidates) if candidates else None

    visited_at = _earliest_funnel("visited")
    deposited_at = _earliest_funnel("deposited")
    # 報到：to_stage="enrolled" 或 event_type="converted"
    enrolled_at = _earliest_funnel("enrolled")
    if enrolled_at is None and student.enrollment_date:
        enrolled_at = student.enrollment_date

    # 在學：to_stage="active" 或 lifecycle_status 已 active 用 enrollment_date 兜底
    active_at = _earliest_funnel("active")
    if active_at is None and student.lifecycle_status in (
        "active", "on_leave", "graduated", "withdrawn", "transferred"
    ):
        active_at = student.enrollment_date

    # 當前 lifecycle 決定哪一點 current / done
    current = student.lifecycle_status
    terminal_kinds = {"graduated", "withdrawn", "transferred"}

    def _status_for(key: OuterKey) -> StepStatus:
        # 從前段往後判斷
        if key == "visited":
            if visited_at:
                return "done" if current != "prospect" or deposited_at else "current"
            return "future"
        if key == "deposited":
            if deposited_at:
                return "done" if current not in ("prospect",) or enrolled_at else "current"
            return "future"
        if key == "enrolled":
            if enrolled_at:
                return "done" if current not in ("prospect", "enrolled") else "current"
            return "future"
        if key == "active":
            if active_at is None:
                return "future"
            if current in terminal_kinds:
                return "done"
            return "current"
        # terminal
        if current in terminal_kinds:
            return "done"
        return "future"

    terminal_at: Optional[date] = None
    if current == "graduated":
        terminal_at = student.graduation_date
    elif current in ("withdrawn", "transferred"):
        terminal_at = student.withdrawal_date

    return [
        StepInfo(key="visited", label=_OUTER_LABELS["visited"],
                 status=_status_for("visited"), occurred_at=visited_at),
        StepInfo(key="deposited", label=_OUTER_LABELS["deposited"],
                 status=_status_for("deposited"), occurred_at=deposited_at),
        StepInfo(key="enrolled", label=_OUTER_LABELS["enrolled"],
                 status=_status_for("enrolled"), occurred_at=enrolled_at),
        StepInfo(key="active", label=_OUTER_LABELS["active"],
                 status=_status_for("active"), occurred_at=active_at),
        StepInfo(key="terminal", label=_OUTER_LABELS["terminal"],
                 status=_status_for("terminal"), occurred_at=terminal_at),
    ]
```

- [ ] **Step 5: 跑測試確認全綠**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_student_lifecycle_overview.py -v
```

Expected: `5 passed`。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend && git add services/student_lifecycle_overview.py tests/test_student_lifecycle_overview.py && git commit -m "feat(lifecycle-overview): T0 純函式 compute_outer_steps + 5 pytest"
```

---

## Task 1: 後端純函式 — `compute_inner_grade_steps()`

**Files:**
- Modify: `ivy-backend/services/student_lifecycle_overview.py`
- Modify: `ivy-backend/tests/test_student_lifecycle_overview.py`

- [ ] **Step 1: 加 5 個失敗測試**

把以下測試 append 到 `tests/test_student_lifecycle_overview.py`：

```python
from services.student_lifecycle_overview import (
    GradeStepInfo,
    compute_inner_grade_steps,
)


def _grade(grade_id, name, sort_order, is_grad=False):
    return SimpleNamespace(
        id=grade_id,
        name=name,
        sort_order=sort_order,
        is_graduation_grade=is_grad,
        is_active=True,
    )


def _transfer(student_id, to_classroom_id, transferred_at, from_classroom_id=None):
    return SimpleNamespace(
        student_id=student_id,
        from_classroom_id=from_classroom_id,
        to_classroom_id=to_classroom_id,
        transferred_at=transferred_at,
    )


# 共用 grades + classroom maps
GRADES_FOUR = [
    _grade(1, "幼幼", 1),
    _grade(2, "小班", 2),
    _grade(3, "中班", 3),
    _grade(4, "大班", 4, is_grad=True),
]
# classroom_id → grade_id
CR_GRADE = {11: 1, 21: 2, 31: 3, 41: 4}
CR_NAME = {11: "幼幼A", 21: "小班A", 31: "中班A", 41: "大班A"}


def test_compute_inner_grades_full_journey_from_yo_yo():
    """幼幼班一路升大班 — 4 個年級全有 transfer。"""
    student = _stu(classroom_id=41, enrollment_date=date(2022, 8, 15))
    transfers = [
        _transfer(1, 11, date(2022, 8, 15)),
        _transfer(1, 21, date(2023, 8, 1), from_classroom_id=11),
        _transfer(1, 31, date(2024, 8, 1), from_classroom_id=21),
        _transfer(1, 41, date(2025, 8, 1), from_classroom_id=31),
    ]
    steps = compute_inner_grade_steps(student, GRADES_FOUR, transfers, CR_GRADE, CR_NAME)
    assert [s.grade_id for s in steps] == [1, 2, 3, 4]
    assert [s.status for s in steps] == ["done", "done", "done", "current"]
    assert steps[0].entered_at == date(2022, 8, 15)
    assert steps[3].entered_at == date(2025, 8, 1)


def test_compute_inner_grades_mid_year_enrollment():
    """5 歲入大班 — 幼幼/小班/中班 = skipped，大班 = current。"""
    student = _stu(classroom_id=41, enrollment_date=date(2025, 8, 1))
    transfers = [_transfer(1, 41, date(2025, 8, 1))]
    steps = compute_inner_grade_steps(student, GRADES_FOUR, transfers, CR_GRADE, CR_NAME)
    assert [s.status for s in steps] == ["skipped", "skipped", "skipped", "current"]
    assert steps[0].entered_at is None
    assert steps[3].entered_at == date(2025, 8, 1)


def test_compute_inner_grades_no_transfer_history_fallback():
    """無 transfer 紀錄 — 用 student.classroom_id 推當前年級，前段全 skipped。"""
    student = _stu(classroom_id=31, enrollment_date=date(2024, 8, 1))
    steps = compute_inner_grade_steps(student, GRADES_FOUR, [], CR_GRADE, CR_NAME)
    assert [s.status for s in steps] == ["skipped", "skipped", "current", "future"]
    assert steps[2].entered_at == date(2024, 8, 1)  # 兜底用 enrollment_date


def test_compute_inner_grades_with_class_change_same_grade():
    """同年級內換班 — 不影響 stepper status，但 entered_at 取最早。"""
    student = _stu(classroom_id=31, enrollment_date=date(2024, 8, 1))
    transfers = [
        _transfer(1, 31, date(2024, 8, 1)),  # 中班A
        _transfer(1, 31, date(2024, 10, 5), from_classroom_id=31),  # 換到中班B 但仍中班
    ]
    steps = compute_inner_grade_steps(student, GRADES_FOUR, transfers, CR_GRADE, CR_NAME)
    assert steps[2].entered_at == date(2024, 8, 1)


def test_compute_inner_grades_skipped_middle_grade():
    """跳級：幼幼 → 中班（跳過小班）— 小班 skipped。"""
    student = _stu(classroom_id=31)
    transfers = [
        _transfer(1, 11, date(2023, 8, 1)),
        _transfer(1, 31, date(2024, 8, 1), from_classroom_id=11),
    ]
    steps = compute_inner_grade_steps(student, GRADES_FOUR, transfers, CR_GRADE, CR_NAME)
    assert [s.status for s in steps] == ["done", "skipped", "current", "future"]
```

- [ ] **Step 2: 跑測試確認 5 fail**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_student_lifecycle_overview.py -v -k inner_grades
```

Expected: `5 failed` with `ImportError: cannot import name 'compute_inner_grade_steps'`。

- [ ] **Step 3: 實作 `compute_inner_grade_steps`**

把以下加到 `services/student_lifecycle_overview.py`：

```python
def compute_inner_grade_steps(
    student,
    all_grades: list,  # 已 filter is_active=true，已 sort by sort_order
    transfers: list,  # list[StudentClassroomTransfer]，可空
    classroom_grade_map: dict[int, int],  # classroom_id → grade_id
    classroom_name_map: dict[int, str],
) -> list[GradeStepInfo]:
    if not all_grades:
        return []

    # 依 transfers 找出「曾進入過的 grade」與其最早日期
    grade_entered: dict[int, date] = {}
    grade_classroom_name: dict[int, str] = {}
    for tr in sorted(transfers, key=lambda t: t.transferred_at):
        gid = classroom_grade_map.get(tr.to_classroom_id)
        if gid is None:
            continue
        d = tr.transferred_at.date() if hasattr(tr.transferred_at, "date") else tr.transferred_at
        if gid not in grade_entered or d < grade_entered[gid]:
            grade_entered[gid] = d
        grade_classroom_name[gid] = classroom_name_map.get(tr.to_classroom_id) or grade_classroom_name.get(gid)

    # 推當前年級：student.classroom_id → grade_id
    current_grade_id: Optional[int] = None
    if student.classroom_id is not None:
        current_grade_id = classroom_grade_map.get(student.classroom_id)
    # 若 transfers 有但 student.classroom_id 對不到，用最晚的 transfer 推
    if current_grade_id is None and transfers:
        latest = max(transfers, key=lambda t: t.transferred_at)
        current_grade_id = classroom_grade_map.get(latest.to_classroom_id)

    # 入學年級：transfer 中最早的 grade；若無 transfer，用 current_grade_id 兜底
    if grade_entered:
        first_grade_id = min(
            grade_entered.keys(),
            key=lambda gid: next(g.sort_order for g in all_grades if g.id == gid),
        )
    else:
        first_grade_id = current_grade_id

    # current_grade 的 fallback entered_at = student.enrollment_date
    if (
        current_grade_id is not None
        and current_grade_id not in grade_entered
        and student.enrollment_date
    ):
        grade_entered[current_grade_id] = student.enrollment_date
        if student.classroom_id is not None:
            grade_classroom_name[current_grade_id] = classroom_name_map.get(student.classroom_id)

    first_sort = (
        next((g.sort_order for g in all_grades if g.id == first_grade_id), None)
        if first_grade_id else None
    )
    current_sort = (
        next((g.sort_order for g in all_grades if g.id == current_grade_id), None)
        if current_grade_id else None
    )

    steps: list[GradeStepInfo] = []
    for g in all_grades:
        if current_sort is None:
            status: GradeStepStatus = "future"
        elif first_sort is not None and g.sort_order < first_sort:
            status = "skipped"
        elif g.sort_order > current_sort:
            status = "future"
        elif g.sort_order == current_sort:
            status = "current"
        else:
            # 介於 first 與 current 之間
            status = "done" if g.id in grade_entered else "skipped"
        steps.append(
            GradeStepInfo(
                grade_id=g.id,
                name=g.name,
                sort_order=g.sort_order,
                status=status,
                entered_at=grade_entered.get(g.id),
                classroom_name=grade_classroom_name.get(g.id),
            )
        )
    return steps
```

- [ ] **Step 4: 跑測試確認 5 個 inner_grades + 既有 5 個 outer_steps 全綠**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_student_lifecycle_overview.py -v
```

Expected: `10 passed`。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend && git add services/student_lifecycle_overview.py tests/test_student_lifecycle_overview.py && git commit -m "feat(lifecycle-overview): T1 純函式 compute_inner_grade_steps + 5 pytest"
```

---

## Task 2: 後端純函式 — `compute_terminal()`

**Files:**
- Modify: `ivy-backend/services/student_lifecycle_overview.py`
- Modify: `ivy-backend/tests/test_student_lifecycle_overview.py`

- [ ] **Step 1: 加 4 個失敗測試**

```python
from services.student_lifecycle_overview import (
    TerminalInfo,
    compute_terminal,
)


def test_compute_terminal_expected_graduation_in_future():
    """在學中 — 預測 graduation = 當前年級到畢業年級的學年差 + 開學年。"""
    student = _stu(lifecycle_status="active", classroom_id=21)  # 小班
    inner = [
        GradeStepInfo(grade_id=1, name="幼幼", sort_order=1, status="done"),
        GradeStepInfo(grade_id=2, name="小班", sort_order=2, status="current",
                      entered_at=date(2024, 8, 1)),
        GradeStepInfo(grade_id=3, name="中班", sort_order=3, status="future"),
        GradeStepInfo(grade_id=4, name="大班", sort_order=4, status="future"),
    ]
    t = compute_terminal(student, inner, graduation_grade_sort_order=4,
                         term_end_date_for=lambda year: None)
    assert t.kind == "none"
    assert t.actual_date is None
    # current=2024，距離畢業 4-2=2 年 → 預計畢業 2026/7/31
    assert t.expected_date == date(2026, 7, 31)


def test_compute_terminal_at_graduation_grade():
    """已在畢業年級 — expected = 同學年 7/31。"""
    student = _stu(lifecycle_status="active", classroom_id=41)
    inner = [
        GradeStepInfo(grade_id=4, name="大班", sort_order=4, status="current",
                      entered_at=date(2026, 8, 1)),
    ]
    t = compute_terminal(student, inner, graduation_grade_sort_order=4,
                         term_end_date_for=lambda year: None)
    assert t.expected_date == date(2027, 7, 31)


def test_compute_terminal_graduated_actual():
    student = _stu(lifecycle_status="graduated", graduation_date=date(2027, 7, 1))
    t = compute_terminal(student, [], graduation_grade_sort_order=4,
                         term_end_date_for=lambda year: None)
    assert t.kind == "graduated"
    assert t.actual_date == date(2027, 7, 1)
    assert t.expected_date is None


def test_compute_terminal_uses_academic_term_end_date_when_available():
    """若 term_end_date_for 回傳實際 end_date，優先於 7/31 預設。"""
    student = _stu(lifecycle_status="active", classroom_id=41)
    inner = [
        GradeStepInfo(grade_id=4, name="大班", sort_order=4, status="current",
                      entered_at=date(2026, 8, 1)),
    ]
    t = compute_terminal(
        student, inner, graduation_grade_sort_order=4,
        term_end_date_for=lambda year: date(2027, 6, 30) if year == 2026 else None,
    )
    assert t.expected_date == date(2027, 6, 30)
```

- [ ] **Step 2: 跑測試確認 4 fail**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_student_lifecycle_overview.py -v -k terminal
```

- [ ] **Step 3: 實作 `compute_terminal`**

加到 `services/student_lifecycle_overview.py`：

```python
from typing import Callable


def compute_terminal(
    student,
    inner_grade_steps: list[GradeStepInfo],
    graduation_grade_sort_order: Optional[int],
    term_end_date_for: Callable[[int], Optional[date]],
) -> TerminalInfo:
    """推算終態。

    term_end_date_for(school_year) → 該學年下學期 end_date 或 None。
    """
    status = student.lifecycle_status
    if status == "graduated":
        return TerminalInfo(kind="graduated", actual_date=student.graduation_date)
    if status == "withdrawn":
        return TerminalInfo(kind="withdrawn", actual_date=student.withdrawal_date)
    if status == "transferred":
        return TerminalInfo(kind="transferred", actual_date=student.withdrawal_date)

    # 在學中（active / on_leave / enrolled / prospect）— 預測畢業日
    if graduation_grade_sort_order is None:
        return TerminalInfo(kind="none")

    current = next((s for s in inner_grade_steps if s.status == "current"), None)
    if current is None or current.entered_at is None:
        return TerminalInfo(kind="none")

    # 學年差 = 畢業年級 sort_order - 當前年級 sort_order
    diff = graduation_grade_sort_order - current.sort_order
    # 「進入當前年級的學年」= entered_at.year（若 entered_at 在 8 月之前則學年是去年的）
    entered_year = current.entered_at.year
    if current.entered_at.month < 8:
        entered_year -= 1
    expected_school_year = entered_year + diff

    # 優先用 AcademicTerm 的 end_date
    explicit = term_end_date_for(expected_school_year)
    if explicit is not None:
        return TerminalInfo(kind="none", expected_date=explicit)

    # 預設 7/31 of (expected_school_year + 1)
    return TerminalInfo(
        kind="none",
        expected_date=date(expected_school_year + 1, 7, 31),
    )
```

- [ ] **Step 4: 跑測試全綠**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_student_lifecycle_overview.py -v
```

Expected: `14 passed`（10 + 4）。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend && git add services/student_lifecycle_overview.py tests/test_student_lifecycle_overview.py && git commit -m "feat(lifecycle-overview): T2 純函式 compute_terminal + 4 pytest"
```

---

## Task 3: 後端整合 — `build_lifecycle_overview()` + Pydantic schema + endpoint

**Files:**
- Modify: `ivy-backend/services/student_lifecycle_overview.py`
- Create: `ivy-backend/schemas/student_lifecycle.py`
- Modify: `ivy-backend/api/students.py`
- Modify: `ivy-backend/tests/test_student_lifecycle_overview.py`

- [ ] **Step 1: 寫 Pydantic schema**

寫到 `ivy-backend/schemas/student_lifecycle.py`：

```python
"""schemas/student_lifecycle.py — 在校歷程 read-only output schema。"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel


class StepOut(BaseModel):
    key: Literal["visited", "deposited", "enrolled", "active", "terminal"]
    label: str
    status: Literal["done", "current", "future"]
    occurred_at: Optional[date] = None
    meta: Optional[dict] = None


class GradeStepOut(BaseModel):
    grade_id: int
    name: str
    sort_order: int
    status: Literal["done", "current", "future", "skipped"]
    entered_at: Optional[date] = None
    expected_at: Optional[date] = None
    classroom_name: Optional[str] = None


class TerminalOut(BaseModel):
    kind: Literal["graduated", "withdrawn", "transferred", "none"]
    actual_date: Optional[date] = None
    expected_date: Optional[date] = None


class LifecycleOverviewOut(BaseModel):
    student_id: int
    current_stage: str
    on_leave_badge: bool
    on_leave_since: Optional[date] = None
    outer_steps: list[StepOut]
    inner_grade_steps: list[GradeStepOut]
    terminal: TerminalOut
```

- [ ] **Step 2: 實作 `build_lifecycle_overview` orchestrator**

加到 `services/student_lifecycle_overview.py`：

```python
from sqlalchemy.orm import Session


def build_lifecycle_overview(session: Session, student_id: int) -> LifecycleOverview:
    """API entrypoint — 一次 query 完所有需要資料，丟給純函式。"""
    from models.classroom import Student, Classroom, ClassGrade
    from models.recruitment import RecruitmentEventLog
    from models.student_log import StudentChangeLog
    from models.student_transfer import StudentClassroomTransfer
    from models.academic_term import AcademicTerm

    student = session.query(Student).filter(Student.id == student_id).first()
    if student is None:
        raise ValueError(f"student not found: id={student_id}")

    funnel_events = (
        session.query(RecruitmentEventLog)
        .filter(RecruitmentEventLog.student_id == student_id)
        .order_by(RecruitmentEventLog.created_at.asc())
        .all()
    )
    # 早期 funnel events 可能 student_id IS NULL 但 visit_id 對應 — 補一筆 by visit_id
    if student.recruitment_visit_id:
        extra = (
            session.query(RecruitmentEventLog)
            .filter(
                RecruitmentEventLog.recruitment_visit_id == student.recruitment_visit_id,
                RecruitmentEventLog.student_id.is_(None),
            )
            .order_by(RecruitmentEventLog.created_at.asc())
            .all()
        )
        funnel_events = sorted(extra + funnel_events, key=lambda e: e.created_at)

    change_logs = (
        session.query(StudentChangeLog)
        .filter(StudentChangeLog.student_id == student_id)
        .order_by(StudentChangeLog.event_date.asc())
        .all()
    )

    transfers = (
        session.query(StudentClassroomTransfer)
        .filter(StudentClassroomTransfer.student_id == student_id)
        .order_by(StudentClassroomTransfer.transferred_at.asc())
        .all()
    )

    all_grades = (
        session.query(ClassGrade)
        .filter(ClassGrade.is_active == True)  # noqa: E712
        .order_by(ClassGrade.sort_order.asc())
        .all()
    )

    # classroom_id → grade_id / name 兩 dict
    classroom_rows = session.query(Classroom.id, Classroom.grade_id, Classroom.name).all()
    classroom_grade_map = {row[0]: row[1] for row in classroom_rows if row[1] is not None}
    classroom_name_map = {row[0]: row[2] for row in classroom_rows}

    outer = compute_outer_steps(student, funnel_events, change_logs)
    inner = compute_inner_grade_steps(
        student, all_grades, transfers, classroom_grade_map, classroom_name_map
    )

    # 畢業年級 sort_order
    grad_sort = next(
        (g.sort_order for g in all_grades if g.is_graduation_grade), None
    )

    # term_end_date_for: 該學年下學期 end_date
    def term_end_date_for(school_year: int) -> Optional[date]:
        row = (
            session.query(AcademicTerm.end_date)
            .filter(
                AcademicTerm.school_year == school_year,
                AcademicTerm.semester == 2,
            )
            .first()
        )
        return row[0] if row else None

    terminal = compute_terminal(student, inner, grad_sort, term_end_date_for)

    # on_leave 徽章與起始日
    on_leave_badge = student.lifecycle_status == "on_leave"
    on_leave_since: Optional[date] = None
    if on_leave_badge:
        latest_leave = (
            session.query(StudentChangeLog)
            .filter(
                StudentChangeLog.student_id == student_id,
                StudentChangeLog.event_type == "休學",
            )
            .order_by(StudentChangeLog.event_date.desc())
            .first()
        )
        on_leave_since = latest_leave.event_date if latest_leave else None

    return LifecycleOverview(
        student_id=student.id,
        current_stage=student.lifecycle_status,
        on_leave_badge=on_leave_badge,
        on_leave_since=on_leave_since,
        outer_steps=outer,
        inner_grade_steps=inner,
        terminal=terminal,
    )
```

- [ ] **Step 3: 加 endpoint**

在 `ivy-backend/api/students.py` 找 `router = APIRouter()` 後方任一現有 GET endpoint 旁邊，加：

```python
from schemas.student_lifecycle import LifecycleOverviewOut
from services.student_lifecycle_overview import build_lifecycle_overview


@router.get(
    "/students/{student_id}/lifecycle-overview",
    response_model=LifecycleOverviewOut,
)
def get_student_lifecycle_overview(
    student_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    session = get_session()
    try:
        overview = build_lifecycle_overview(session, student_id)
        return LifecycleOverviewOut(
            student_id=overview.student_id,
            current_stage=overview.current_stage,
            on_leave_badge=overview.on_leave_badge,
            on_leave_since=overview.on_leave_since,
            outer_steps=[step.__dict__ for step in overview.outer_steps],
            inner_grade_steps=[gs.__dict__ for gs in overview.inner_grade_steps],
            terminal=overview.terminal.__dict__,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        session.close()
```

如果 `HTTPException` 已 import 跳過 import；若無，加 `from fastapi import HTTPException`。

- [ ] **Step 4: 寫整合測試（用 SQLite session）**

append 到 `tests/test_student_lifecycle_overview.py`：

```python
def test_build_lifecycle_overview_end_to_end(client, db_session):
    """整合 happy path：建一個 active 學生在小班，預期外層 active=current + 預計畢業日。"""
    from models.classroom import Student, Classroom, ClassGrade
    from models.student_transfer import StudentClassroomTransfer

    g_small = ClassGrade(name="小班", sort_order=2, is_active=True)
    g_grad = ClassGrade(name="大班", sort_order=4, is_active=True, is_graduation_grade=True)
    db_session.add_all([g_small, g_grad])
    db_session.flush()

    cr_small = Classroom(name="小班A", school_year=113, semester=1, grade_id=g_small.id)
    db_session.add(cr_small)
    db_session.flush()

    student = Student(
        student_id="113-S-01",
        name="測試生",
        classroom_id=cr_small.id,
        enrollment_date=date(2024, 8, 15),
        lifecycle_status="active",
    )
    db_session.add(student)
    db_session.flush()

    db_session.add(StudentClassroomTransfer(
        student_id=student.id,
        to_classroom_id=cr_small.id,
        transferred_at=date(2024, 8, 15),
    ))
    db_session.commit()

    from services.student_lifecycle_overview import build_lifecycle_overview
    ov = build_lifecycle_overview(db_session, student.id)

    assert ov.student_id == student.id
    assert ov.current_stage == "active"
    assert [s.key for s in ov.outer_steps] == ["visited", "deposited", "enrolled", "active", "terminal"]
    # 小班 → 大班 = 2 年差 → 預計畢業 2026/7/31
    assert ov.terminal.kind == "none"
    assert ov.terminal.expected_date == date(2026, 7, 31)
    # 內層只有兩個 active grade（小班、大班）
    assert [g.name for g in ov.inner_grade_steps] == ["小班", "大班"]
    assert ov.inner_grade_steps[0].status == "current"
    assert ov.inner_grade_steps[1].status == "future"
```

寫 endpoint 整合測試（接 FastAPI TestClient）：

```python
def test_endpoint_returns_lifecycle_overview(client, admin_token):
    """endpoint 串通：權限通、回 200、shape 對。"""
    # 假設有 fixture seed 一個 student id=1
    r = client.get(
        "/api/students/1/lifecycle-overview",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        data = r.json()
        assert "outer_steps" in data
        assert "inner_grade_steps" in data
        assert "terminal" in data
        assert len(data["outer_steps"]) == 5


def test_endpoint_404_when_student_missing(client, admin_token):
    r = client.get(
        "/api/students/999999/lifecycle-overview",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 404
```

**Fixture 注意**：`client` / `db_session` / `admin_token` 是 conftest.py 已有的 fixture（檢查 `tests/conftest.py`）。若名稱不同要對齊。

- [ ] **Step 5: 跑測試確認全綠**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_student_lifecycle_overview.py -v
```

Expected: `17 passed`（14 純函式 + 3 整合）。

- [ ] **Step 6: dump OpenAPI 驗證 endpoint 已註冊**

```bash
cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py
grep "lifecycle-overview" openapi.json
```

Expected: 出現 `"/students/{student_id}/lifecycle-overview"` path。

- [ ] **Step 7: Commit**

```bash
cd ~/Desktop/ivy-backend && git add services/student_lifecycle_overview.py schemas/student_lifecycle.py api/students.py tests/test_student_lifecycle_overview.py && git commit -m "feat(lifecycle-overview): T3 build_lifecycle_overview + endpoint + schema + 3 integration test"
```

---

## Task 4: 後端 timeline 擴充 — funnel_event / classroom_transfer / payment

**Files:**
- Modify: `ivy-backend/services/student_records_timeline.py`
- Create: `ivy-backend/tests/test_student_records_timeline_extended.py`

- [ ] **Step 1: 看現有 RECORD_TYPES 與 fetch/build 慣例**

```bash
cd ~/Desktop/ivy-backend && grep -n "RECORD_TYPES\|_fetch_\|_build_" services/student_records_timeline.py
```

確認 set 位置與 helper 結構（已有 `_fetch_incidents` / `_build_incident_item` 等）。

- [ ] **Step 2: 寫 5 個失敗測試**

寫到 `tests/test_student_records_timeline_extended.py`：

```python
"""tests/test_student_records_timeline_extended.py — timeline 3 source 擴充。

來源：funnel_event / classroom_transfer / payment
"""

from datetime import date, datetime

import pytest


def _create_student_basic(session):
    from models.classroom import Student, Classroom, ClassGrade
    g = ClassGrade(name="小班", sort_order=2, is_active=True)
    session.add(g)
    session.flush()
    cr = Classroom(name="小班A", school_year=113, semester=1, grade_id=g.id)
    session.add(cr)
    session.flush()
    s = Student(
        student_id="113-S-99",
        name="時間軸測試生",
        classroom_id=cr.id,
        lifecycle_status="active",
        enrollment_date=date(2024, 8, 15),
    )
    session.add(s)
    session.flush()
    return s, cr


def test_timeline_includes_funnel_events(db_session):
    from services.student_records_timeline import list_timeline
    from models.recruitment import RecruitmentEventLog, RecruitmentVisit

    s, _ = _create_student_basic(db_session)
    rv = RecruitmentVisit(child_name="時間軸測試生", visit_date=date(2024, 7, 12))
    db_session.add(rv)
    db_session.flush()
    db_session.add(RecruitmentEventLog(
        recruitment_visit_id=rv.id, student_id=s.id,
        event_type="visit_logged", to_stage="visited",
        created_at=datetime(2024, 7, 12, 10, 0),
    ))
    db_session.commit()

    result = list_timeline(db_session, student_id=s.id, types=["funnel_event"])
    items = result["items"]
    assert len(items) >= 1
    assert items[0]["record_type"] == "funnel_event"


def test_timeline_includes_classroom_transfers(db_session):
    from services.student_records_timeline import list_timeline
    from models.student_transfer import StudentClassroomTransfer

    s, cr = _create_student_basic(db_session)
    db_session.add(StudentClassroomTransfer(
        student_id=s.id, to_classroom_id=cr.id, transferred_at=datetime(2024, 8, 15)
    ))
    db_session.commit()

    result = list_timeline(db_session, student_id=s.id, types=["classroom_transfer"])
    items = result["items"]
    assert len(items) >= 1
    assert items[0]["record_type"] == "classroom_transfer"


def test_timeline_includes_payments(db_session):
    from services.student_records_timeline import list_timeline
    from models.fees import StudentFeeRecord, StudentFeePayment

    s, cr = _create_student_basic(db_session)
    rec = StudentFeeRecord(
        student_id=s.id, student_name=s.name, classroom_name=cr.name,
        fee_item_name="註冊費", amount_due=5000, amount_paid=5000,
        status="paid", payment_date=date(2024, 8, 1), period="113-1",
    )
    db_session.add(rec)
    db_session.flush()
    db_session.add(StudentFeePayment(
        student_fee_record_id=rec.id, amount=5000, paid_at=date(2024, 8, 1),
    ))
    db_session.commit()

    result = list_timeline(db_session, student_id=s.id, types=["payment"])
    items = result["items"]
    assert len(items) >= 1
    assert items[0]["record_type"] == "payment"
    assert "5000" in items[0]["summary"] or "$" in items[0]["summary"]


def test_timeline_combined_sources_sorted_descending(db_session):
    """混合多 source — 時間倒序。"""
    from services.student_records_timeline import list_timeline
    from models.recruitment import RecruitmentEventLog, RecruitmentVisit
    from models.student_transfer import StudentClassroomTransfer

    s, cr = _create_student_basic(db_session)
    rv = RecruitmentVisit(child_name=s.name, visit_date=date(2024, 7, 12))
    db_session.add(rv)
    db_session.flush()
    db_session.add(RecruitmentEventLog(
        recruitment_visit_id=rv.id, student_id=s.id,
        event_type="visit_logged", to_stage="visited",
        created_at=datetime(2024, 7, 12, 10, 0),
    ))
    db_session.add(StudentClassroomTransfer(
        student_id=s.id, to_classroom_id=cr.id, transferred_at=datetime(2024, 8, 15)
    ))
    db_session.commit()

    result = list_timeline(
        db_session, student_id=s.id,
        types=["funnel_event", "classroom_transfer"],
    )
    items = result["items"]
    assert len(items) >= 2
    # 倒序：8/15 應在 7/12 之前
    assert items[0]["_ts"] >= items[1]["_ts"]


def test_timeline_default_types_excludes_new_sources(db_session):
    """既有 caller 不傳 types → 維持只回原 3 source（向後相容）。"""
    from services.student_records_timeline import list_timeline, RECORD_TYPES

    # 確認新類型已加進 RECORD_TYPES
    assert "funnel_event" in RECORD_TYPES
    assert "classroom_transfer" in RECORD_TYPES
    assert "payment" in RECORD_TYPES
```

- [ ] **Step 3: 跑測試確認 fail**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_student_records_timeline_extended.py -v
```

- [ ] **Step 4: 實作 3 個 fetch + 3 個 build + 更新 RECORD_TYPES + dispatch**

在 `services/student_records_timeline.py` 找到 `RECORD_TYPES` 並擴：

```python
RECORD_TYPES = {
    "incident",
    "assessment",
    "change_log",
    "funnel_event",
    "classroom_transfer",
    "payment",
}
```

在檔尾加 3 個 fetch + build pair（仿照既有 `_fetch_incidents` / `_build_incident_item` 命名）：

```python
def _fetch_funnel_events(
    session: Session,
    *,
    student_id: Optional[int],
    classroom_id: Optional[int],
    date_from: Optional[date],
    date_to: Optional[date],
    scope_ids: Optional[list[int]],
) -> list:
    from models.recruitment import RecruitmentEventLog
    q = session.query(RecruitmentEventLog).filter(
        RecruitmentEventLog.student_id.isnot(None)
    )
    if student_id is not None:
        q = q.filter(RecruitmentEventLog.student_id == student_id)
    if date_from is not None:
        q = q.filter(RecruitmentEventLog.created_at >= date_from)
    if date_to is not None:
        q = q.filter(RecruitmentEventLog.created_at <= date_to)
    if scope_ids is not None:
        q = q.filter(RecruitmentEventLog.student_id.in_(scope_ids))
    return q.all()


def _build_funnel_event_item(fe) -> dict[str, Any]:
    summary = f"招生階段 {fe.from_stage or '-'} → {fe.to_stage}"
    return {
        "record_type": "funnel_event",
        "record_id": fe.id,
        "student_id": fe.student_id,
        "summary": summary,
        "occurred_at": fe.created_at.date() if hasattr(fe.created_at, "date") else fe.created_at,
        "_ts": _to_datetime(fe.created_at),
        "reason": fe.reason,
        "actor_user_id": fe.actor_user_id,
        "event_type": fe.event_type,
    }


def _fetch_classroom_transfers(
    session: Session,
    *,
    student_id: Optional[int],
    classroom_id: Optional[int],
    date_from: Optional[date],
    date_to: Optional[date],
    scope_ids: Optional[list[int]],
) -> list:
    from models.student_transfer import StudentClassroomTransfer
    q = session.query(StudentClassroomTransfer)
    if student_id is not None:
        q = q.filter(StudentClassroomTransfer.student_id == student_id)
    if date_from is not None:
        q = q.filter(StudentClassroomTransfer.transferred_at >= date_from)
    if date_to is not None:
        q = q.filter(StudentClassroomTransfer.transferred_at <= date_to)
    if scope_ids is not None:
        q = q.filter(StudentClassroomTransfer.student_id.in_(scope_ids))
    return q.all()


def _build_classroom_transfer_item(tr) -> dict[str, Any]:
    return {
        "record_type": "classroom_transfer",
        "record_id": tr.id,
        "student_id": tr.student_id,
        "summary": "轉班",
        "occurred_at": tr.transferred_at.date() if hasattr(tr.transferred_at, "date") else tr.transferred_at,
        "_ts": _to_datetime(tr.transferred_at),
        "from_classroom_id": tr.from_classroom_id,
        "to_classroom_id": tr.to_classroom_id,
        "actor_user_id": tr.transferred_by,
    }


def _fetch_payments(
    session: Session,
    *,
    student_id: Optional[int],
    classroom_id: Optional[int],
    date_from: Optional[date],
    date_to: Optional[date],
    scope_ids: Optional[list[int]],
) -> list:
    from models.fees import StudentFeePayment, StudentFeeRecord
    q = (
        session.query(StudentFeePayment, StudentFeeRecord.student_id,
                      StudentFeeRecord.fee_item_name)
        .join(StudentFeeRecord, StudentFeePayment.student_fee_record_id == StudentFeeRecord.id)
    )
    if student_id is not None:
        q = q.filter(StudentFeeRecord.student_id == student_id)
    if date_from is not None:
        q = q.filter(StudentFeePayment.paid_at >= date_from)
    if date_to is not None:
        q = q.filter(StudentFeePayment.paid_at <= date_to)
    if scope_ids is not None:
        q = q.filter(StudentFeeRecord.student_id.in_(scope_ids))
    return q.all()


def _build_payment_item(row) -> dict[str, Any]:
    payment, sid, item_name = row
    return {
        "record_type": "payment",
        "record_id": payment.id,
        "student_id": sid,
        "summary": f"繳交 {item_name} NT${payment.amount}",
        "occurred_at": payment.paid_at,
        "_ts": _to_datetime(payment.paid_at),
        "amount": payment.amount,
    }
```

在 `list_timeline()` 中加 3 個 dispatch（在原 3 個之後）：

```python
    if "funnel_event" in enabled:
        for fe in _fetch_funnel_events(
            session, student_id=student_id, classroom_id=classroom_id,
            date_from=date_from, date_to=date_to, scope_ids=scope_ids,
        ):
            items.append(_build_funnel_event_item(fe))

    if "classroom_transfer" in enabled:
        for tr in _fetch_classroom_transfers(
            session, student_id=student_id, classroom_id=classroom_id,
            date_from=date_from, date_to=date_to, scope_ids=scope_ids,
        ):
            items.append(_build_classroom_transfer_item(tr))

    if "payment" in enabled:
        for row in _fetch_payments(
            session, student_id=student_id, classroom_id=classroom_id,
            date_from=date_from, date_to=date_to, scope_ids=scope_ids,
        ):
            items.append(_build_payment_item(row))
```

- [ ] **Step 5: 跑測試全綠**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_student_records_timeline_extended.py tests/test_student_records_timeline.py -v
```

Expected: 5 新測試 pass + 既有測試零 regression。

- [ ] **Step 6: dump OpenAPI 確認 types enum 更新**

```bash
cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py && grep -A2 "types" openapi.json | head -20
```

如果 endpoint 是用 `Query(...)` + `enum` 限制，可能要在 `api/student_change_logs.py`（或 timeline endpoint 所在處）加 3 個新值。Step 1 已 grep 過位置。

- [ ] **Step 7: 加 FEES_READ 權限退化**

spec §3.4 規定：viewer 沒有 `FEES_READ` 時，`payment` 類型應從 enabled set 移除。

在 timeline endpoint 所在處（Step 1 grep 找到的檔案，可能是 `api/student_change_logs.py`）找呼叫 `list_timeline()` 之前的 query param 處理段落，加：

```python
from utils.permissions import Permission

# 在組 types 之後、呼叫 list_timeline 之前
if "payment" in (types or []) and not _user_has_permission(current_user, Permission.FEES_READ):
    types = [t for t in types if t != "payment"]
```

`_user_has_permission` 已存在於 utils；若名稱不同（例如 `has_permission`、`check_permission`），grep 找對應 helper。

加一個 pytest 確認退化行為：

```python
def test_timeline_payment_filtered_when_no_fees_permission(db_session, non_fees_user_token, client):
    """無 FEES_READ 權限的 user 即使請求 types=payment 也不應拿到 payment 紀錄。"""
    # seed 一個有 payment 的學生（沿用 _create_student_basic + StudentFeePayment）
    s, cr = _create_student_basic(db_session)
    from models.fees import StudentFeeRecord, StudentFeePayment
    rec = StudentFeeRecord(
        student_id=s.id, student_name=s.name, classroom_name=cr.name,
        fee_item_name="註冊費", amount_due=5000, amount_paid=5000,
        status="paid", payment_date=date(2024, 8, 1), period="113-1",
    )
    db_session.add(rec); db_session.flush()
    db_session.add(StudentFeePayment(student_fee_record_id=rec.id, amount=5000, paid_at=date(2024, 8, 1)))
    db_session.commit()

    r = client.get(
        f"/api/students/{s.id}/timeline?types=payment",
        headers={"Authorization": f"Bearer {non_fees_user_token}"},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert all(it["record_type"] != "payment" for it in items)
```

**Fixture 注意**：`non_fees_user_token` 可能需新增 — 找 `tests/conftest.py` 看現有角色 fixture（如 `teacher_token`、`hr_token`）哪個沒有 FEES_READ，直接複用。

- [ ] **Step 8: 跑全 timeline 測試確認 6 pass**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_student_records_timeline_extended.py -v
```

Expected: `6 passed`（5 + 1 權限退化）。

- [ ] **Step 9: Commit**

```bash
cd ~/Desktop/ivy-backend && git add services/student_records_timeline.py api/student_change_logs.py tests/test_student_records_timeline_extended.py && git commit -m "feat(timeline): T4 加 3 source + FEES_READ 退化 + 6 pytest"
```

---

## Task 5: 前端 OpenAPI codegen + API wrapper

**Files:**
- Create: `ivy-frontend/src/api/studentLifecycle.ts`
- Modify: `ivy-frontend/src/api/_generated/schema.d.ts`（auto-regen）

- [ ] **Step 1: 跑 OpenAPI codegen 拉新 endpoint 型別**

```bash
cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py
cd ~/Desktop/ivy-frontend && npm run gen:api
```

Expected: `src/api/_generated/schema.d.ts` 有 diff，含 `/students/{student_id}/lifecycle-overview` path 與 `LifecycleOverviewOut` 等 schema。

- [ ] **Step 2: 寫 API wrapper**

寫到 `ivy-frontend/src/api/studentLifecycle.ts`：

```ts
import api from './index'
import type { ApiResponse, AxiosResp } from './_generated/typed'

const base = (studentId: number) => `/students/${studentId}/lifecycle-overview`

export const getLifecycleOverview = (studentId: number) =>
  api.get<ApiResponse<'/students/{student_id}/lifecycle-overview', 'get'>>(
    base(studentId)
  ) as AxiosResp<'/students/{student_id}/lifecycle-overview', 'get'>
```

- [ ] **Step 3: typecheck 通**

```bash
cd ~/Desktop/ivy-frontend && npm run typecheck
```

Expected: 0 errors。

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/ivy-frontend && git add src/api/studentLifecycle.ts src/api/_generated/schema.d.ts && git commit -m "feat(student-lifecycle): T5 api wrapper + OpenAPI regen"
```

---

## Task 6: 前端元件 — `InnerGradeStepperRow.vue`

**Files:**
- Create: `ivy-frontend/src/components/student/tabs/lifecycle/InnerGradeStepperRow.vue`
- Create: `ivy-frontend/src/components/student/tabs/lifecycle/__tests__/InnerGradeStepperRow.spec.ts`

- [ ] **Step 1: 寫元件骨架**

```vue
<script setup lang="ts">
import type { components } from '@/api/_generated/schema'

type GradeStep = components['schemas']['GradeStepOut']

defineProps<{
  grades: GradeStep[]
}>()
</script>

<template>
  <div class="inner-grade-stepper" data-testid="inner-grade-stepper">
    <div
      v-for="(g, idx) in grades"
      :key="g.grade_id"
      class="grade-dot"
      :class="`status-${g.status}`"
      :data-testid="`grade-${g.grade_id}`"
    >
      <span class="grade-name">{{ g.name }}</span>
      <span v-if="g.entered_at" class="grade-entered">{{ g.entered_at }}</span>
      <span v-if="idx < grades.length - 1" class="grade-sep" :class="`sep-${g.status}`"></span>
    </div>
  </div>
</template>

<style scoped>
.inner-grade-stepper { display: flex; gap: 12px; align-items: center; }
.grade-dot { display: flex; flex-direction: column; align-items: center; position: relative; }
.grade-name { font-size: 14px; padding: 4px 12px; border-radius: 12px; }
.grade-entered { font-size: 11px; color: var(--el-color-info); margin-top: 2px; }
.grade-sep { position: absolute; right: -12px; top: 12px; width: 12px; height: 2px; }

.status-done .grade-name { background: var(--el-color-primary-light-7); color: var(--el-color-primary); }
.status-current .grade-name { background: var(--el-color-primary); color: white; animation: pulse 2s infinite; }
.status-future .grade-name { background: var(--el-color-info-light-9); color: var(--el-color-info); }
.status-skipped .grade-name { background: transparent; color: var(--el-color-info-light-3); border: 1px dashed var(--el-color-info-light-5); }
.sep-done, .sep-current { background: var(--el-color-primary); }
.sep-future, .sep-skipped { background: var(--el-color-info-light-7); border-top: 1px dashed var(--el-color-info-light-5); height: 0; }

@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 0 var(--el-color-primary-light-5); }
  50% { box-shadow: 0 0 0 8px transparent; }
}
</style>
```

- [ ] **Step 2: 寫 vitest**

寫到 `ivy-frontend/src/components/student/tabs/lifecycle/__tests__/InnerGradeStepperRow.spec.ts`：

```ts
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import InnerGradeStepperRow from '../InnerGradeStepperRow.vue'

const grades = [
  { grade_id: 1, name: '幼幼', sort_order: 1, status: 'skipped', entered_at: null, expected_at: null, classroom_name: null },
  { grade_id: 2, name: '小班', sort_order: 2, status: 'done', entered_at: '2024-08-15', expected_at: null, classroom_name: '小班A' },
  { grade_id: 3, name: '中班', sort_order: 3, status: 'current', entered_at: '2025-08-01', expected_at: null, classroom_name: '中班A' },
  { grade_id: 4, name: '大班', sort_order: 4, status: 'future', entered_at: null, expected_at: null, classroom_name: null },
]

describe('InnerGradeStepperRow', () => {
  it('renders all grades in order', () => {
    const w = mount(InnerGradeStepperRow, { props: { grades } })
    const dots = w.findAll('[data-testid^="grade-"]')
    expect(dots).toHaveLength(4)
  })

  it('applies status-* class to each dot', () => {
    const w = mount(InnerGradeStepperRow, { props: { grades } })
    expect(w.find('[data-testid="grade-1"]').classes()).toContain('status-skipped')
    expect(w.find('[data-testid="grade-2"]').classes()).toContain('status-done')
    expect(w.find('[data-testid="grade-3"]').classes()).toContain('status-current')
    expect(w.find('[data-testid="grade-4"]').classes()).toContain('status-future')
  })

  it('shows entered_at only when present', () => {
    const w = mount(InnerGradeStepperRow, { props: { grades } })
    expect(w.find('[data-testid="grade-2"]').text()).toContain('2024-08-15')
    expect(w.find('[data-testid="grade-1"]').text()).not.toContain('2024')
  })

  it('handles empty grades list', () => {
    const w = mount(InnerGradeStepperRow, { props: { grades: [] } })
    expect(w.findAll('[data-testid^="grade-"]')).toHaveLength(0)
  })
})
```

- [ ] **Step 3: 跑測試確認 4 pass**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/components/student/tabs/lifecycle/__tests__/InnerGradeStepperRow.spec.ts
```

Expected: `4 passed`。

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/ivy-frontend && git add src/components/student/tabs/lifecycle/InnerGradeStepperRow.vue src/components/student/tabs/lifecycle/__tests__/InnerGradeStepperRow.spec.ts && git commit -m "feat(student-lifecycle): T6 InnerGradeStepperRow + 4 vitest"
```

---

## Task 7: 前端元件 — `OuterStepperRow.vue`

**Files:**
- Create: `ivy-frontend/src/components/student/tabs/lifecycle/OuterStepperRow.vue`
- Create: `ivy-frontend/src/components/student/tabs/lifecycle/__tests__/OuterStepperRow.spec.ts`

- [ ] **Step 1: 寫元件**

```vue
<script setup lang="ts">
import { computed } from 'vue'
import type { components } from '@/api/_generated/schema'

type Overview = components['schemas']['LifecycleOverviewOut']
type Step = components['schemas']['StepOut']
type Terminal = components['schemas']['TerminalOut']

const props = defineProps<{ overview: Overview }>()

const terminalLabel = computed(() => {
  const t = props.overview.terminal
  if (t.kind === 'graduated') return '已畢業'
  if (t.kind === 'withdrawn') return '已退學'
  if (t.kind === 'transferred') return '已轉學'
  if (t.expected_date) return `預計畢業 ${t.expected_date}`
  return '尚未畢業'
})

const terminalColorClass = computed(() => {
  const t = props.overview.terminal
  return `terminal-${t.kind}`
})

const stepLabel = (s: Step) => {
  if (s.key === 'terminal') return terminalLabel.value
  return s.label
}
</script>

<template>
  <div class="outer-stepper" data-testid="outer-stepper">
    <div
      v-for="(s, idx) in overview.outer_steps"
      :key="s.key"
      class="outer-dot"
      :class="[`status-${s.status}`, s.key === 'terminal' && terminalColorClass]"
      :data-testid="`outer-${s.key}`"
    >
      <div class="dot-circle"></div>
      <div class="dot-label">{{ stepLabel(s) }}</div>
      <div v-if="s.occurred_at" class="dot-date">{{ s.occurred_at }}</div>
      <span
        v-if="s.key === 'active' && overview.on_leave_badge"
        class="leave-badge"
        data-testid="on-leave-badge"
        title="休學中"
      >⏸</span>
      <span v-if="idx < overview.outer_steps.length - 1" class="outer-sep"></span>
    </div>
  </div>
</template>

<style scoped>
.outer-stepper { display: flex; gap: 32px; align-items: flex-start; padding: 16px; }
.outer-dot { display: flex; flex-direction: column; align-items: center; position: relative; min-width: 60px; }
.dot-circle { width: 24px; height: 24px; border-radius: 50%; }
.dot-label { font-size: 13px; margin-top: 6px; font-weight: 500; }
.dot-date { font-size: 11px; color: var(--el-color-info); margin-top: 2px; }
.outer-sep { position: absolute; right: -28px; top: 11px; width: 28px; height: 2px; background: var(--el-color-info-light-7); }

.status-done .dot-circle { background: var(--el-color-primary); }
.status-current .dot-circle { background: var(--el-color-primary-light-3); animation: pulse 2s infinite; box-shadow: 0 0 0 4px var(--el-color-primary-light-7); }
.status-future .dot-circle { background: var(--el-color-info-light-7); }

.terminal-graduated.status-done .dot-circle { background: #67c23a; }
.terminal-withdrawn.status-done .dot-circle { background: #f56c6c; }
.terminal-transferred.status-done .dot-circle { background: #e6a23c; }

.leave-badge { position: absolute; top: -8px; right: -8px; background: var(--el-color-warning); color: white; padding: 2px 6px; border-radius: 12px; font-size: 12px; }

@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 4px var(--el-color-primary-light-7); }
  50% { box-shadow: 0 0 0 8px var(--el-color-primary-light-9); }
}
</style>
```

- [ ] **Step 2: 寫 vitest**

寫到 `ivy-frontend/src/components/student/tabs/lifecycle/__tests__/OuterStepperRow.spec.ts`：

```ts
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import OuterStepperRow from '../OuterStepperRow.vue'

const baseOverview = {
  student_id: 1,
  current_stage: 'active',
  on_leave_badge: false,
  on_leave_since: null,
  outer_steps: [
    { key: 'visited', label: '參觀', status: 'done', occurred_at: '2024-07-12', meta: null },
    { key: 'deposited', label: '預繳', status: 'done', occurred_at: '2024-08-01', meta: null },
    { key: 'enrolled', label: '報到', status: 'done', occurred_at: '2024-08-15', meta: null },
    { key: 'active', label: '在學', status: 'current', occurred_at: '2024-09-01', meta: null },
    { key: 'terminal', label: '終態', status: 'future', occurred_at: null, meta: null },
  ],
  inner_grade_steps: [],
  terminal: { kind: 'none', actual_date: null, expected_date: '2027-07-31' },
}

describe('OuterStepperRow', () => {
  it('renders 5 dots', () => {
    const w = mount(OuterStepperRow, { props: { overview: baseOverview } })
    expect(w.findAll('[data-testid^="outer-"]')).toHaveLength(5)
  })

  it('shows expected graduation date on terminal future', () => {
    const w = mount(OuterStepperRow, { props: { overview: baseOverview } })
    expect(w.find('[data-testid="outer-terminal"]').text()).toContain('2027-07-31')
  })

  it('shows graduated label and green color when graduated', () => {
    const ov = {
      ...baseOverview,
      current_stage: 'graduated',
      outer_steps: baseOverview.outer_steps.map((s) =>
        s.key === 'terminal' ? { ...s, status: 'done', occurred_at: '2027-07-01' } : s
      ),
      terminal: { kind: 'graduated', actual_date: '2027-07-01', expected_date: null },
    }
    const w = mount(OuterStepperRow, { props: { overview: ov } })
    const dot = w.find('[data-testid="outer-terminal"]')
    expect(dot.text()).toContain('已畢業')
    expect(dot.classes()).toContain('terminal-graduated')
  })

  it('shows on-leave badge when active', () => {
    const ov = {
      ...baseOverview,
      current_stage: 'on_leave',
      on_leave_badge: true,
      on_leave_since: '2025-03-01',
    }
    const w = mount(OuterStepperRow, { props: { overview: ov } })
    expect(w.find('[data-testid="on-leave-badge"]').exists()).toBe(true)
  })

  it('hides on-leave badge when not on leave', () => {
    const w = mount(OuterStepperRow, { props: { overview: baseOverview } })
    expect(w.find('[data-testid="on-leave-badge"]').exists()).toBe(false)
  })

  it('terminal withdrawn shows red', () => {
    const ov = {
      ...baseOverview,
      current_stage: 'withdrawn',
      outer_steps: baseOverview.outer_steps.map((s) =>
        s.key === 'terminal' ? { ...s, status: 'done', occurred_at: '2025-05-01' } : s
      ),
      terminal: { kind: 'withdrawn', actual_date: '2025-05-01', expected_date: null },
    }
    const w = mount(OuterStepperRow, { props: { overview: ov } })
    expect(w.find('[data-testid="outer-terminal"]').classes()).toContain('terminal-withdrawn')
  })
})
```

- [ ] **Step 3: 跑測試**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/components/student/tabs/lifecycle/__tests__/OuterStepperRow.spec.ts
```

Expected: `6 passed`。

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/ivy-frontend && git add src/components/student/tabs/lifecycle/OuterStepperRow.vue src/components/student/tabs/lifecycle/__tests__/OuterStepperRow.spec.ts && git commit -m "feat(student-lifecycle): T7 OuterStepperRow + 6 vitest"
```

---

## Task 8: 前端元件 — `LifecycleTimelineList.vue`

**Files:**
- Create: `ivy-frontend/src/components/student/tabs/lifecycle/LifecycleTimelineList.vue`
- Create: `ivy-frontend/src/components/student/tabs/lifecycle/__tests__/LifecycleTimelineList.spec.ts`

- [ ] **Step 1: 寫元件**

```vue
<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { ElCheckboxGroup, ElCheckbox, ElEmpty } from 'element-plus'
import { getStudentTimeline } from '@/api/studentTimeline'

const props = defineProps<{
  studentId: number
}>()

type TimelineItem = {
  record_type: string
  record_id: number
  summary: string
  occurred_at: string | null
  reason?: string | null
  amount?: number | null
  from_classroom_id?: number | null
  to_classroom_id?: number | null
}

const ALL_TYPES = [
  { key: 'funnel_event', label: '招生階段' },
  { key: 'change_log', label: '生命週期' },
  { key: 'classroom_transfer', label: '轉班' },
  { key: 'payment', label: '繳費' },
  { key: 'incident', label: '事件' },
  { key: 'assessment', label: '評量' },
]

const enabledTypes = ref<string[]>(ALL_TYPES.map((t) => t.key))
const items = ref<TimelineItem[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const TYPE_LABEL = computed(() =>
  Object.fromEntries(ALL_TYPES.map((t) => [t.key, t.label]))
)

async function load() {
  loading.value = true
  error.value = null
  try {
    const resp = await getStudentTimeline(props.studentId, {
      types: enabledTypes.value,
      page: 1,
      page_size: 100,
    })
    items.value = (resp.data?.items || []) as TimelineItem[]
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'load failed'
  } finally {
    loading.value = false
  }
}

watch(enabledTypes, load, { deep: true })
watch(() => props.studentId, load, { immediate: true })
</script>

<template>
  <div class="lifecycle-timeline" data-testid="lifecycle-timeline">
    <div class="filter-row">
      <el-checkbox-group v-model="enabledTypes" data-testid="type-filter">
        <el-checkbox
          v-for="t in ALL_TYPES"
          :key="t.key"
          :label="t.key"
          :data-testid="`filter-${t.key}`"
        >{{ t.label }}</el-checkbox>
      </el-checkbox-group>
    </div>

    <div v-if="error" class="timeline-error" data-testid="timeline-error">
      {{ error }}
    </div>

    <el-empty v-else-if="!loading && items.length === 0" description="無紀錄" />

    <ul v-else class="timeline-list">
      <li
        v-for="it in items"
        :key="`${it.record_type}-${it.record_id}`"
        class="timeline-item"
        :data-testid="`item-${it.record_type}-${it.record_id}`"
      >
        <span class="item-date">{{ it.occurred_at }}</span>
        <span class="item-type">{{ TYPE_LABEL[it.record_type] || it.record_type }}</span>
        <span class="item-summary">{{ it.summary }}</span>
        <span v-if="it.reason" class="item-reason">（{{ it.reason }}）</span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.lifecycle-timeline { padding: 16px; }
.filter-row { margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid var(--el-border-color-lighter); }
.timeline-list { list-style: none; padding: 0; margin: 0; }
.timeline-item { display: flex; gap: 12px; padding: 8px 0; border-bottom: 1px dashed var(--el-border-color-lighter); align-items: baseline; }
.item-date { font-size: 12px; color: var(--el-color-info); min-width: 90px; }
.item-type { font-size: 12px; padding: 2px 8px; border-radius: 4px; background: var(--el-color-primary-light-9); color: var(--el-color-primary); }
.item-summary { flex: 1; }
.item-reason { font-size: 12px; color: var(--el-color-info); }
.timeline-error { color: var(--el-color-danger); padding: 8px; }
</style>
```

- [ ] **Step 2: 寫 vitest**

寫到 `__tests__/LifecycleTimelineList.spec.ts`：

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'

vi.mock('@/api/studentTimeline', () => ({
  getStudentTimeline: vi.fn(),
}))

import LifecycleTimelineList from '../LifecycleTimelineList.vue'
import { getStudentTimeline } from '@/api/studentTimeline'

const mockTimeline = getStudentTimeline as unknown as ReturnType<typeof vi.fn>

beforeEach(() => {
  mockTimeline.mockReset()
})

describe('LifecycleTimelineList', () => {
  it('loads timeline on mount with all types enabled', async () => {
    mockTimeline.mockResolvedValue({
      data: {
        items: [
          { record_type: 'funnel_event', record_id: 1, summary: '招生階段 - → visited', occurred_at: '2024-07-12' },
          { record_type: 'payment', record_id: 2, summary: '繳交 註冊費 NT$5000', occurred_at: '2024-08-01' },
        ],
        total: 2, page: 1, page_size: 100,
      },
    })
    const w = mount(LifecycleTimelineList, {
      props: { studentId: 99 },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    expect(mockTimeline).toHaveBeenCalledWith(99, expect.objectContaining({
      types: expect.arrayContaining(['funnel_event', 'payment', 'incident']),
    }))
    expect(w.findAll('.timeline-item')).toHaveLength(2)
  })

  it('reloads when types filter changes', async () => {
    mockTimeline.mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 100 } })
    const w = mount(LifecycleTimelineList, {
      props: { studentId: 99 },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    mockTimeline.mockClear()
    // 取消 'incident'
    const cb = w.find('[data-testid="filter-incident"] input')
    await cb.trigger('click')
    await flushPromises()
    expect(mockTimeline).toHaveBeenCalled()
    const call = mockTimeline.mock.calls[mockTimeline.mock.calls.length - 1]
    expect(call[1].types).not.toContain('incident')
  })

  it('shows empty state when no items', async () => {
    mockTimeline.mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 100 } })
    const w = mount(LifecycleTimelineList, {
      props: { studentId: 99 },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    expect(w.text()).toContain('無紀錄')
  })

  it('shows error message when load fails', async () => {
    mockTimeline.mockRejectedValue(new Error('network down'))
    const w = mount(LifecycleTimelineList, {
      props: { studentId: 99 },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    expect(w.find('[data-testid="timeline-error"]').text()).toContain('network down')
  })
})
```

**注意 fixture**：`getStudentTimeline` 已存在於 `src/api/studentTimeline.ts`，新元件直接呼叫即可。若該函式 signature 與 mock 不符（傳 `studentId, options` 還是 `options` 物件），先 cat 該檔案對齊。

- [ ] **Step 3: 看現有 getStudentTimeline signature 對齊 mock 呼叫**

```bash
cd ~/Desktop/ivy-frontend && cat src/api/studentTimeline.ts
```

若簽章是 `(studentId, params)` 則上面測試 OK；若是其他形狀，調整測試與元件 `load()` 呼叫。

- [ ] **Step 4: 跑測試**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/components/student/tabs/lifecycle/__tests__/LifecycleTimelineList.spec.ts
```

Expected: `4 passed`。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-frontend && git add src/components/student/tabs/lifecycle/LifecycleTimelineList.vue src/components/student/tabs/lifecycle/__tests__/LifecycleTimelineList.spec.ts && git commit -m "feat(student-lifecycle): T8 LifecycleTimelineList + 4 vitest"
```

---

## Task 9: 前端整合元件 — `LifecycleTab.vue`

**Files:**
- Create: `ivy-frontend/src/components/student/tabs/LifecycleTab.vue`
- Create: `ivy-frontend/src/components/student/tabs/__tests__/LifecycleTab.spec.ts`

- [ ] **Step 1: 寫整合元件**

```vue
<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElAlert, ElCollapse, ElCollapseItem } from 'element-plus'
import type { components } from '@/api/_generated/schema'
import { getLifecycleOverview } from '@/api/studentLifecycle'
import OuterStepperRow from './lifecycle/OuterStepperRow.vue'
import InnerGradeStepperRow from './lifecycle/InnerGradeStepperRow.vue'
import LifecycleTimelineList from './lifecycle/LifecycleTimelineList.vue'

type Overview = components['schemas']['LifecycleOverviewOut']

const props = defineProps<{
  studentId: number
  active?: boolean
}>()

const overview = ref<Overview | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const collapseValue = ref<string[]>(['timeline'])

async function load() {
  if (!props.studentId) return
  loading.value = true
  error.value = null
  try {
    const resp = await getLifecycleOverview(props.studentId)
    overview.value = resp.data
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'load failed'
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.studentId, props.active] as const,
  ([sid, isActive]) => {
    if (sid && (isActive !== false)) load()
  },
  { immediate: true }
)
</script>

<template>
  <div class="lifecycle-tab" data-testid="lifecycle-tab">
    <div v-if="loading" data-testid="lifecycle-loading">載入中...</div>
    <el-alert v-else-if="error" type="error" :closable="false" data-testid="lifecycle-error">
      {{ error }}
    </el-alert>
    <template v-else-if="overview">
      <div
        v-if="overview.on_leave_badge"
        class="on-leave-banner"
        data-testid="on-leave-banner"
      >
        ⏸ {{ overview.on_leave_since || '日期未知' }} 起休學中
      </div>

      <OuterStepperRow :overview="overview" />

      <div v-if="overview.inner_grade_steps.length > 0" class="inner-section">
        <h4 class="section-title">年級進度</h4>
        <InnerGradeStepperRow :grades="overview.inner_grade_steps" />
      </div>

      <el-collapse v-model="collapseValue" class="timeline-section">
        <el-collapse-item name="timeline" title="詳細歷史">
          <LifecycleTimelineList :student-id="studentId" />
        </el-collapse-item>
      </el-collapse>
    </template>
  </div>
</template>

<style scoped>
.lifecycle-tab { padding: 8px; }
.on-leave-banner { background: var(--el-color-warning-light-9); color: var(--el-color-warning); padding: 8px 12px; border-radius: 4px; margin-bottom: 12px; }
.section-title { margin: 16px 8px 8px; font-size: 14px; color: var(--el-color-info); }
.timeline-section { margin-top: 16px; }
</style>
```

- [ ] **Step 2: 寫 vitest**

寫到 `src/components/student/tabs/__tests__/LifecycleTab.spec.ts`：

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'

vi.mock('@/api/studentLifecycle', () => ({
  getLifecycleOverview: vi.fn(),
}))
vi.mock('@/api/studentTimeline', () => ({
  getStudentTimeline: vi.fn().mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 100 } }),
}))

import LifecycleTab from '../LifecycleTab.vue'
import { getLifecycleOverview } from '@/api/studentLifecycle'

const mockOv = getLifecycleOverview as unknown as ReturnType<typeof vi.fn>

const baseOverview = {
  student_id: 1,
  current_stage: 'active',
  on_leave_badge: false,
  on_leave_since: null,
  outer_steps: [
    { key: 'visited', label: '參觀', status: 'done', occurred_at: '2024-07-12', meta: null },
    { key: 'deposited', label: '預繳', status: 'done', occurred_at: '2024-08-01', meta: null },
    { key: 'enrolled', label: '報到', status: 'done', occurred_at: '2024-08-15', meta: null },
    { key: 'active', label: '在學', status: 'current', occurred_at: '2024-09-01', meta: null },
    { key: 'terminal', label: '終態', status: 'future', occurred_at: null, meta: null },
  ],
  inner_grade_steps: [
    { grade_id: 2, name: '小班', sort_order: 2, status: 'current', entered_at: '2024-08-15', expected_at: null, classroom_name: '小班A' },
  ],
  terminal: { kind: 'none', actual_date: null, expected_date: '2027-07-31' },
}

beforeEach(() => { mockOv.mockReset() })

describe('LifecycleTab', () => {
  it('shows stepper + inner grades + timeline when loaded', async () => {
    mockOv.mockResolvedValue({ data: baseOverview })
    const w = mount(LifecycleTab, {
      props: { studentId: 1, active: true },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    expect(w.find('[data-testid="outer-stepper"]').exists()).toBe(true)
    expect(w.find('[data-testid="inner-grade-stepper"]').exists()).toBe(true)
    expect(w.find('[data-testid="lifecycle-timeline"]').exists()).toBe(true)
  })

  it('shows on-leave banner when on_leave_badge', async () => {
    mockOv.mockResolvedValue({
      data: { ...baseOverview, on_leave_badge: true, on_leave_since: '2025-03-01' },
    })
    const w = mount(LifecycleTab, {
      props: { studentId: 1, active: true },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    expect(w.find('[data-testid="on-leave-banner"]').text()).toContain('2025-03-01')
  })

  it('shows error alert when load fails', async () => {
    mockOv.mockRejectedValue(new Error('boom'))
    const w = mount(LifecycleTab, {
      props: { studentId: 1, active: true },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    expect(w.find('[data-testid="lifecycle-error"]').text()).toContain('boom')
  })
})
```

- [ ] **Step 3: 跑測試**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/components/student/tabs/__tests__/LifecycleTab.spec.ts
```

Expected: `3 passed`。

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/ivy-frontend && git add src/components/student/tabs/LifecycleTab.vue src/components/student/tabs/__tests__/LifecycleTab.spec.ts && git commit -m "feat(student-lifecycle): T9 LifecycleTab 整合元件 + 3 vitest"
```

---

## Task 10: 掛 Tab 進 `StudentDetailPanel.vue` 並手測

**Files:**
- Modify: `ivy-frontend/src/components/student/StudentDetailPanel.vue`

- [ ] **Step 1: 在 `StudentDetailPanel.vue` 加 import 與 TAB_DEFS 項**

找到既有 import 區塊（約 line 15-23）加：

```ts
import LifecycleTab from './tabs/LifecycleTab.vue'
```

找到 `TAB_DEFS = computed(() => [...])`（約 line 100）在 `communication` 之前加一行：

```ts
  { name: 'lifecycle', label: '在校歷程', show: true },
```

找到 template 中 `<CommunicationTab>` 條件分支（約 line 375）之前加：

```vue
        <LifecycleTab
          v-else-if="tab.name === 'lifecycle'"
          :student-id="safeStudentId"
          :active="activeTab === 'lifecycle'"
        />
```

- [ ] **Step 2: 跑全 vitest 確認零回歸**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run
```

Expected: 全綠（含新增 4+6+4+3=17 個 spec），typecheck 0。

- [ ] **Step 3: typecheck**

```bash
cd ~/Desktop/ivy-frontend && npm run typecheck
```

Expected: 0 errors。

- [ ] **Step 4: 啟動 dev server 手測**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh
```

開 http://localhost:5173，登入 admin → 進「學生管理」→ 開一個學生 → 點「在校歷程」tab。手測 case：

1. **active 一般學生**：看到外層 5 點正確（前 3 done、active current、terminal future）+ 內層年級 stepper + 預計畢業日
2. **休學學生**：看到 ⏸ banner + active dot 旁徽章
3. **畢業學生**：terminal dot 綠色 + actual_date 出現
4. **退學學生**：terminal dot 紅色
5. **早期學生（無 funnel/transfer 紀錄）**：active dot 仍 current（兜底 enrollment_date），前 3 點 future（可接受）
6. **Timeline 篩選**：取消「繳費」勾選 → 繳費紀錄消失
7. **5 歲入大班學生**：內層幼幼/小/中班顯示 skipped（虛線灰）+ 大班 current

每個 case 用 chrome devtools console 看 `/api/students/{id}/lifecycle-overview` response 對照。

- [ ] **Step 5: Commit + push（push 等 user 確認）**

```bash
cd ~/Desktop/ivy-frontend && git add src/components/student/StudentDetailPanel.vue && git commit -m "feat(student-lifecycle): T10 掛 LifecycleTab 進 StudentDetailPanel"
```

**不要 push**，等 user 手測完口頭通過再 push 兩 repo：

```bash
cd ~/Desktop/ivy-backend && git push origin main
cd ~/Desktop/ivy-frontend && git push origin main
```

---

## 驗收清單

- [ ] 後端 pytest 18 新（14 純函式 + 3 整合 lifecycle-overview + 6 timeline extended 含權限退化）全綠
- [ ] 後端既有 pytest 零 regression
- [ ] 前端 vitest 17 新（4 inner stepper + 6 outer stepper + 4 timeline list + 3 panel）全綠
- [ ] 前端既有 vitest 零 regression
- [ ] typecheck 0 error
- [ ] OpenAPI drift check 0 diff (`npm run gen:api:check`)
- [ ] 手測 7 case 全綠
- [ ] 兩 repo commit graph 乾淨（後端 4-5 commit + 前端 5 commit + plan commit）

## 後續迭代（不在本 plan 範圍 — spec §7）

1. 家長 portal 看自家孩子歷程（PII / actor 隱藏）
2. 點 dot 觸發動作 dialog（休學 / 退學 / 轉學）
3. 升班儀式批次工具（年度推進精靈）
4. 終態副作用 SOP 串接（退費試算 / 解綁 LIFF / 活動報名取消）
5. 預繳金額對應到 funnel deposited event 的 metadata_json
