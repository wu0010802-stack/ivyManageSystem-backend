# 員工詳情「班級歷程」分頁 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在員工詳情彈窗新增「班級歷程」分頁，顯示員工歷年（學年×學期）帶過的班級、角色（導師/助教）、同班搭檔，與班級期初/期末人數（資訊等級，無快照則留白）。

**Architecture:** 後端新增一個唯讀端點 `GET /api/employees/{id}/class-history`，核心邏輯放在新 service `services/employee_class_history.py`（router 保持薄）。資歷主幹從 `Classroom` 的 teacher FK 反查（可靠）；人數從 `MonthlyEnrollmentSnapshot`（過去學期）或即時在籍數（當前班）取得，無資料即 `None`。前端在 `EmployeeView.vue` 沿用既有 lazy-load 分頁模式新增一頁，格式化邏輯抽到 `src/utils/classHistory.ts` 以便 vitest。

**Tech Stack:** FastAPI + SQLAlchemy + PostgreSQL（測試 SQLite in-memory）；Vue 3 `<script setup lang="ts">` + Element Plus + Vitest；OpenAPI→TS codegen。

設計 spec：`docs/superpowers/specs/2026-06-06-employee-class-history-design.md`

---

## File Structure

**後端（worktree `feat/employee-class-history-2026-06-06-be`，自 origin/main `f29850cf`）：**
- Modify: `schemas/employees.py` — 新增 `ClassHistoryCoTeacher` / `ClassHistoryRow` / `ClassHistoryResponse`
- Create: `services/employee_class_history.py` — 核心查詢與人數計算
- Modify: `api/employees.py` — 新增 router endpoint
- Create: `tests/test_employees_class_history.py` — service + endpoint 測試

**前端（worktree `feat/employee-class-history-2026-06-06-fe`，自 origin/main）：**
- Modify: `src/api/_generated/schema.d.ts` — `npm run gen:api` 重新產生（後端端點就緒後）
- Modify: `src/api/employees.ts` — 新增 `listEmployeeClassHistory`
- Create: `src/utils/classHistory.ts` — 純格式化 helper
- Create: `src/utils/__tests__/classHistory.spec.ts` — vitest
- Modify: `src/views/EmployeeView.vue` — 新增分頁 + lazy-load wiring + el-table

---

## 後端

### Task 1: 新增 Pydantic schemas

**Files:**
- Modify: `schemas/employees.py`（在檔尾，現有 `from typing import Optional` 與 `from schemas._base import IvyBaseModel` 已 import）

- [ ] **Step 1: 在 `schemas/employees.py` 檔尾新增 schemas**

先確認檔案頂部 import 區有 `Literal`。若 `from typing import Optional` 那行未含 `Literal`，改為：

```python
from typing import Literal, Optional
```

在檔案最後新增：

```python
class ClassHistoryCoTeacher(IvyBaseModel):
    """同班搭檔老師（含才藝）。"""

    role: Literal["head", "assistant", "art"]
    employee_id: int
    name: str


class ClassHistoryRow(IvyBaseModel):
    """員工某學期帶的一個班的歷程列。"""

    school_year: int  # 民國學年，例 114
    semester: int  # 1=上學期 2=下學期
    classroom_id: int
    classroom_name: str
    grade_name: Optional[str] = None
    role: Literal["head", "assistant"]  # 此員工在這班的角色（才藝不成列）
    co_teachers: list[ClassHistoryCoTeacher] = []
    is_current: bool = False
    start_count: Optional[int] = None  # 期初人數；None=資料不足
    end_count: Optional[int] = None  # 期末人數；當前學期=即時在籍數
    end_count_is_live: bool = False  # True 時前端顯示「目前 N」
    net_change: Optional[int] = None  # end-start，兩者皆有才算


class ClassHistoryResponse(IvyBaseModel):
    """GET /employees/{id}/class-history 回傳。"""

    rows: list[ClassHistoryRow] = []
```

- [ ] **Step 2: 確認 import 正常**

Run: `cd ~/Desktop/ivy-backend/.claude/worktrees/employee-class-history && python -c "from schemas.employees import ClassHistoryResponse, ClassHistoryRow, ClassHistoryCoTeacher; print('ok')"`
Expected: 印出 `ok`，無 ImportError。

- [ ] **Step 3: Commit**

```bash
git add schemas/employees.py
git commit -m "feat(employees): 班級歷程 response schema"
```

---

### Task 2: 人數計算 helper（service）

**Files:**
- Create: `services/employee_class_history.py`
- Test: `tests/test_employees_class_history.py`

人數規則：當前學期班 → 期末＝即時在籍數（`end_count_is_live=True`）、期初＝開學月快照；過去學期 → 期初/期末皆讀快照；無快照即 `None`。月份用 `term_bounds()` 取年月，避免硬算民國→西元。

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_employees_class_history.py` 新增（檔案開頭含共用 import 與 fixture，見下方完整內容）：

```python
"""tests/test_employees_class_history.py — 員工班級歷程 service + endpoint 測試。"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.employees import router as employees_router
from models.base import Base
from models.database import (
    Classroom,
    Employee,
    Student,
    User,
)
from models.classroom import ClassGrade
from models.gov_moe import MonthlyEnrollmentSnapshot
from services.employee_class_history import _term_headcounts, build_class_history
from utils.auth import hash_password


@pytest.fixture
def db():
    """SQLite in-memory session（swap 全域 engine）。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    s = session_factory()
    try:
        yield s, session_factory
    finally:
        s.close()
        base_module._engine = old_engine
        base_module._SessionFactory = old_sf
        engine.dispose()


def _mk_classroom(s, *, name, school_year, semester, head=None, assistant=None, art=None, grade_id=None):
    c = Classroom(
        name=name,
        school_year=school_year,
        semester=semester,
        head_teacher_id=head,
        assistant_teacher_id=assistant,
        art_teacher_id=art,
        grade_id=grade_id,
    )
    s.add(c)
    s.flush()
    return c


def test_term_headcounts_past_reads_snapshot(db):
    """過去學期：期初讀開學月快照、期末讀期末月快照、跨 age_group 加總。"""
    s, _ = db
    c = _mk_classroom(s, name="葡萄班", school_year=113, semester=2, head=1)
    # 下學期 113-2：開學月=西元(113+1911+1)=2025/2、期末月=2025/7
    s.add_all([
        MonthlyEnrollmentSnapshot(year=2025, month=2, classroom_id=c.id, age_group="3-4", total_count=10),
        MonthlyEnrollmentSnapshot(year=2025, month=2, classroom_id=c.id, age_group="4-5", total_count=12),
        MonthlyEnrollmentSnapshot(year=2025, month=7, classroom_id=c.id, age_group="3-4", total_count=20),
    ])
    s.commit()
    start, end, is_live = _term_headcounts(s, c.id, 113, 2, is_current=False)
    assert start == 22
    assert end == 20
    assert is_live is False


def test_term_headcounts_no_snapshot_returns_none(db):
    """過去學期無快照 → start/end 皆 None。"""
    s, _ = db
    c = _mk_classroom(s, name="無料班", school_year=112, semester=1, head=1)
    s.commit()
    start, end, is_live = _term_headcounts(s, c.id, 112, 1, is_current=False)
    assert start is None
    assert end is None
    assert is_live is False


def test_term_headcounts_current_uses_live_end(db):
    """當前學期：期末=即時在籍數、is_live=True。"""
    s, _ = db
    c = _mk_classroom(s, name="蘋果班", school_year=114, semester=2, head=1)
    s.add_all([
        Student(student_id="S1", name="生一", classroom_id=c.id, enrollment_date=date(2024, 8, 1)),
        Student(student_id="S2", name="生二", classroom_id=c.id, enrollment_date=date(2024, 8, 1)),
    ])
    s.commit()
    start, end, is_live = _term_headcounts(s, c.id, 114, 2, is_current=True)
    assert end == 2
    assert is_live is True
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend/.claude/worktrees/employee-class-history && python -m pytest tests/test_employees_class_history.py -k term_headcounts -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'services.employee_class_history'`

- [ ] **Step 3: 建立 service 與 `_term_headcounts`**

Create `services/employee_class_history.py`：

```python
"""員工班級歷程查詢 service。

資歷主幹（學期×班級×角色×同班搭檔）從 Classroom 的 teacher FK 反查，可靠。
人數為資訊等級：過去學期讀 MonthlyEnrollmentSnapshot 當期快照；當前學期班
期末用即時在籍數。無快照即 None，不做會算錯的轉班回放（見設計 spec §6）。
"""

from __future__ import annotations

from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from models.database import Classroom, Employee
from models.gov_moe import MonthlyEnrollmentSnapshot
from services.student_enrollment import count_students_active_on
from utils.academic import resolve_current_academic_term, term_bounds
from utils.taipei_time import today_taipei


def _snapshot_count(session, classroom_id: int, year: int, month: int) -> int | None:
    """某班某西元年月的快照人數（跨 age_group 加總）；無資料回 None。"""
    total = (
        session.query(func.sum(MonthlyEnrollmentSnapshot.total_count))
        .filter(
            MonthlyEnrollmentSnapshot.classroom_id == classroom_id,
            MonthlyEnrollmentSnapshot.year == year,
            MonthlyEnrollmentSnapshot.month == month,
        )
        .scalar()
    )
    return int(total) if total is not None else None


def _term_headcounts(
    session, classroom_id: int, school_year: int, semester: int, is_current: bool
) -> tuple[int | None, int | None, bool]:
    """回 (start_count, end_count, end_count_is_live)。"""
    start_date, end_date = term_bounds(school_year, semester)
    start_count = _snapshot_count(session, classroom_id, start_date.year, start_date.month)
    if is_current:
        end_count = count_students_active_on(session, today_taipei(), classroom_id)
        return start_count, end_count, True
    end_count = _snapshot_count(session, classroom_id, end_date.year, end_date.month)
    return start_count, end_count, False
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-backend/.claude/worktrees/employee-class-history && python -m pytest tests/test_employees_class_history.py -k term_headcounts -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add services/employee_class_history.py tests/test_employees_class_history.py
git commit -m "feat(employees): 班級歷程人數計算 helper（快照/即時，無料留白）"
```

---

### Task 3: 資歷主幹 `build_class_history`

**Files:**
- Modify: `services/employee_class_history.py`
- Test: `tests/test_employees_class_history.py`

- [ ] **Step 1: 寫失敗測試（接在既有測試檔後）**

```python
def test_build_class_history_spine_excludes_art_and_sorts(db):
    """主幹：含 head/assistant 班、排除純 art 班；依學期由新到舊。"""
    s, _ = db
    teacher = Employee(name="王老師", employee_type="regular")
    other = Employee(name="李助教", employee_type="regular")
    s.add_all([teacher, other])
    s.flush()
    # teacher 當導師（114-2）
    _mk_classroom(s, name="蘋果班", school_year=114, semester=2, head=teacher.id, assistant=other.id)
    # teacher 當助教（113-2）
    _mk_classroom(s, name="葡萄班", school_year=113, semester=2, head=other.id, assistant=teacher.id)
    # teacher 只當才藝（114-1）→ 不應成列
    _mk_classroom(s, name="音樂班", school_year=114, semester=1, head=other.id, art=teacher.id)
    s.commit()

    rows = build_class_history(s, teacher.id)
    # 排除 art-only 班；剩 2 列
    assert [(r["school_year"], r["semester"], r["role"]) for r in rows] == [
        (114, 2, "head"),
        (113, 2, "assistant"),
    ]


def test_build_class_history_co_teachers(db):
    """同班搭檔：含才藝、排除自己、有姓名。"""
    s, _ = db
    me = Employee(name="我", employee_type="regular")
    asst = Employee(name="助教甲", employee_type="regular")
    art = Employee(name="才藝乙", employee_type="regular")
    s.add_all([me, asst, art])
    s.flush()
    _mk_classroom(s, name="蘋果班", school_year=114, semester=2, head=me.id, assistant=asst.id, art=art.id)
    s.commit()

    rows = build_class_history(s, me.id)
    assert len(rows) == 1
    cos = {(c["role"], c["name"]) for c in rows[0]["co_teachers"]}
    assert cos == {("assistant", "助教甲"), ("art", "才藝乙")}
    # 自己不在搭檔內
    assert all(c["employee_id"] != me.id for c in rows[0]["co_teachers"])


def test_build_class_history_net_change_only_when_both_present(db):
    """net_change 僅兩數皆有才算。"""
    s, _ = db
    me = Employee(name="我", employee_type="regular")
    s.add(me)
    s.flush()
    c = _mk_classroom(s, name="葡萄班", school_year=113, semester=2, head=me.id)
    s.add_all([
        MonthlyEnrollmentSnapshot(year=2025, month=2, classroom_id=c.id, age_group="3-4", total_count=22),
        MonthlyEnrollmentSnapshot(year=2025, month=7, classroom_id=c.id, age_group="3-4", total_count=20),
    ])
    s.commit()
    rows = build_class_history(s, me.id)
    assert rows[0]["start_count"] == 22
    assert rows[0]["end_count"] == 20
    assert rows[0]["net_change"] == -2


def test_build_class_history_empty(db):
    """沒帶過任何班 → 空陣列。"""
    s, _ = db
    me = Employee(name="閒人", employee_type="regular")
    s.add(me)
    s.commit()
    assert build_class_history(s, me.id) == []
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend/.claude/worktrees/employee-class-history && python -m pytest tests/test_employees_class_history.py -k build_class_history -v`
Expected: FAIL，`ImportError: cannot import name 'build_class_history'`（或 AttributeError）

- [ ] **Step 3: 實作 `build_class_history`（append 到 service）**

```python
def _resolve_role(classroom: Classroom, employee_id: int) -> str | None:
    """員工在這班的角色：head 優先；art 不成列（回 None）。"""
    if classroom.head_teacher_id == employee_id:
        return "head"
    if classroom.assistant_teacher_id == employee_id:
        return "assistant"
    return None


def build_class_history(session, employee_id: int) -> list[dict]:
    """回該員工的班級歷程列（dict，對齊 ClassHistoryRow shape）。"""
    classrooms = (
        session.query(Classroom)
        .options(joinedload(Classroom.grade))
        .filter(
            or_(
                Classroom.head_teacher_id == employee_id,
                Classroom.assistant_teacher_id == employee_id,
            )
        )
        .order_by(Classroom.school_year.desc(), Classroom.semester.desc())
        .all()
    )
    if not classrooms:
        return []

    # 批次解析所有搭檔姓名，避免 N+1
    teacher_ids: set[int] = set()
    for c in classrooms:
        for tid in (c.head_teacher_id, c.assistant_teacher_id, c.art_teacher_id):
            if tid is not None and tid != employee_id:
                teacher_ids.add(tid)
    name_map: dict[int, str] = {}
    if teacher_ids:
        for emp_id, emp_name in (
            session.query(Employee.id, Employee.name)
            .filter(Employee.id.in_(teacher_ids))
            .all()
        ):
            name_map[emp_id] = emp_name

    cur_year, cur_sem = resolve_current_academic_term()

    rows: list[dict] = []
    for c in classrooms:
        role = _resolve_role(c, employee_id)
        if role is None:
            continue
        is_current = c.school_year == cur_year and c.semester == cur_sem
        start_count, end_count, end_is_live = _term_headcounts(
            session, c.id, c.school_year, c.semester, is_current
        )
        net_change = (
            end_count - start_count
            if start_count is not None and end_count is not None
            else None
        )
        co_teachers = []
        for tid, trole in (
            (c.head_teacher_id, "head"),
            (c.assistant_teacher_id, "assistant"),
            (c.art_teacher_id, "art"),
        ):
            if tid is None or tid == employee_id:
                continue
            co_teachers.append(
                {"role": trole, "employee_id": tid, "name": name_map.get(tid, "")}
            )
        rows.append(
            {
                "school_year": c.school_year,
                "semester": c.semester,
                "classroom_id": c.id,
                "classroom_name": c.name,
                "grade_name": c.grade.name if c.grade else None,
                "role": role,
                "co_teachers": co_teachers,
                "is_current": is_current,
                "start_count": start_count,
                "end_count": end_count,
                "end_count_is_live": end_is_live,
                "net_change": net_change,
            }
        )
    return rows
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-backend/.claude/worktrees/employee-class-history && python -m pytest tests/test_employees_class_history.py -v`
Expected: 全部 passed（含 Task 2 的 3 個 + 本 Task 4 個）

- [ ] **Step 5: Commit**

```bash
git add services/employee_class_history.py tests/test_employees_class_history.py
git commit -m "feat(employees): 班級歷程主幹查詢（角色/搭檔/排序，排除才藝）"
```

---

### Task 4: Router endpoint

**Files:**
- Modify: `api/employees.py`（import 區 + 新 route）
- Test: `tests/test_employees_class_history.py`

- [ ] **Step 1: 寫失敗測試（endpoint 整合，接在測試檔後）**

```python
@pytest.fixture
def client(db):
    s, sf = db
    _ip_attempts.clear()
    _account_failures.clear()
    app = FastAPI()
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(employees_router)
    with TestClient(app) as c:
        yield c, s, sf
    _ip_attempts.clear()
    _account_failures.clear()


def _login_admin(client, sf):
    with sf() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("Temp123456"),
                role="admin",
                permission_names=["EMPLOYEES_READ"],
                employee_id=None,
                is_active=True,
                must_change_password=False,
            )
        )
        s.commit()
    r = client.post("/api/auth/login", json={"username": "admin", "password": "Temp123456"})
    assert r.status_code == 200, r.json()


def test_class_history_endpoint_returns_rows(client):
    c, s, sf = client
    me = Employee(name="王老師", employee_type="regular")
    s.add(me)
    s.flush()
    _mk_classroom(s, name="蘋果班", school_year=114, semester=2, head=me.id)
    s.commit()
    _login_admin(c, sf)

    r = c.get(f"/api/employees/{me.id}/class-history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["role"] == "head"
    assert body["rows"][0]["classroom_name"] == "蘋果班"


def test_class_history_endpoint_requires_permission(client):
    c, s, sf = client
    me = Employee(name="王老師", employee_type="regular")
    s.add(me)
    s.commit()
    # 未登入
    r = c.get(f"/api/employees/{me.id}/class-history")
    assert r.status_code in (401, 403)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend/.claude/worktrees/employee-class-history && python -m pytest tests/test_employees_class_history.py -k endpoint -v`
Expected: FAIL，`404`（route 尚未定義）

- [ ] **Step 3: 加 router**

在 `api/employees.py` 的 `from schemas.employees import (...)` 區塊加入 `ClassHistoryResponse`：

```python
from schemas.employees import (
    ClassHistoryResponse,
    EmployeeCreateResultOut,
    EmployeeOut,
    FinalSalaryPreviewOut,
    MutationResultOut,
    OffboardResultOut,
    ProbationAlertResponseOut,
    TeacherOut,
)
```

在檔案 import 區加入 service import（與其他 `from services...` 同區，若無則加在 utils import 後）：

```python
from services.employee_class_history import build_class_history
```

在 `get_employee` route（`@router.get("/employees/{employee_id}", ...)`）之後新增：

```python
@router.get(
    "/employees/{employee_id}/class-history",
    response_model=ClassHistoryResponse,
)
def get_employee_class_history(
    employee_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_READ)),
):
    """員工班級歷程（學期×班級×角色×搭檔×期初/末人數）。"""
    session = get_session()
    try:
        employee = session.query(Employee).filter(Employee.id == employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail=EMPLOYEE_NOT_FOUND)
        rows = build_class_history(session, employee_id)
        return {"rows": rows}
    finally:
        session.close()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-backend/.claude/worktrees/employee-class-history && python -m pytest tests/test_employees_class_history.py -v`
Expected: 全部 passed

- [ ] **Step 5: 跑相關回歸（確保沒踩到既有 employees 測試）**

Run: `cd ~/Desktop/ivy-backend/.claude/worktrees/employee-class-history && python -m pytest tests/test_employees.py tests/test_employees_class_history.py -q`
Expected: 全 passed

- [ ] **Step 6: Commit**

```bash
git add api/employees.py tests/test_employees_class_history.py
git commit -m "feat(employees): GET /employees/{id}/class-history 端點"
```

---

## 前端

> 前置：在 `~/Desktop/ivy-frontend` 自 origin/main 建立 worktree
> `git worktree add -b feat/employee-class-history-2026-06-06-fe .claude/worktrees/employee-class-history origin/main`
> 並依 MEMORY「前端 worktree node_modules symlink 失效」修好 node_modules（rm 後重建絕對 symlink 指向主 checkout），再 `cd` 進該 worktree 操作。

### Task 5: 重新產生 OpenAPI 型別

**Files:**
- Modify: `src/api/_generated/schema.d.ts`

- [ ] **Step 1: 後端產 openapi.json**

Run（在後端 worktree）：`cd ~/Desktop/ivy-backend/.claude/worktrees/employee-class-history && python scripts/dump_openapi.py`
Expected: 產出 `openapi.json`（local-only）。

- [ ] **Step 2: 前端 gen:api**

把後端剛產的 `openapi.json` 放到前端 codegen 預期位置（依 `npm run gen:api` 慣例；通常讀後端 repo 路徑或本地 openapi.json），執行：
Run: `cd ~/Desktop/ivy-frontend/.claude/worktrees/employee-class-history && npm run gen:api`
Expected: `src/api/_generated/schema.d.ts` 出現 `/employees/{employee_id}/class-history` 的 path 型別。

- [ ] **Step 3: 確認型別存在**

Run: `cd ~/Desktop/ivy-frontend/.claude/worktrees/employee-class-history && grep -c "class-history" src/api/_generated/schema.d.ts`
Expected: ≥ 1

- [ ] **Step 4: Commit**

```bash
git add src/api/_generated/schema.d.ts
git commit -m "chore(api): regen schema.d.ts 含 class-history"
```

---

### Task 6: API wrapper

**Files:**
- Modify: `src/api/employees.ts`

- [ ] **Step 1: 在 `src/api/employees.ts` 既有 educations/certificates/contracts 區塊附近新增**

```typescript
// ========== Class History ==========

export const listEmployeeClassHistory = (id: number): AxiosResp<'/employees/{employee_id}/class-history', 'get'> =>
    api.get(`/employees/${id}/class-history`)
```

- [ ] **Step 2: typecheck**

Run: `cd ~/Desktop/ivy-frontend/.claude/worktrees/employee-class-history && npm run typecheck`
Expected: exit 0（若 schema.d.ts 已含該 path）。

- [ ] **Step 3: Commit**

```bash
git add src/api/employees.ts
git commit -m "feat(employees): 班級歷程 api wrapper"
```

---

### Task 7: 格式化 helper + vitest

**Files:**
- Create: `src/utils/classHistory.ts`
- Test: `src/utils/__tests__/classHistory.spec.ts`

- [ ] **Step 1: 寫失敗測試**

Create `src/utils/__tests__/classHistory.spec.ts`：

```typescript
import { describe, it, expect } from 'vitest'
import {
  formatSemester,
  roleLabel,
  formatCoTeachers,
  formatHeadcount,
  formatNetChange,
  type ClassHistoryRow,
} from '../classHistory'

describe('classHistory formatters', () => {
  it('formatSemester', () => {
    expect(formatSemester(114, 1)).toBe('114 上學期')
    expect(formatSemester(114, 2)).toBe('114 下學期')
  })

  it('roleLabel', () => {
    expect(roleLabel('head')).toBe('導師')
    expect(roleLabel('assistant')).toBe('助教')
    expect(roleLabel('art')).toBe('才藝')
  })

  it('formatCoTeachers', () => {
    expect(
      formatCoTeachers([
        { role: 'assistant', employee_id: 2, name: '李美' },
        { role: 'art', employee_id: 3, name: '陳華' },
      ]),
    ).toBe('助教 李美 · 才藝 陳華')
    expect(formatCoTeachers([])).toBe('—')
  })

  it('formatHeadcount: live current', () => {
    const row = { start_count: 25, end_count: 27, end_count_is_live: true } as ClassHistoryRow
    expect(formatHeadcount(row)).toBe('25 → 目前 27')
  })

  it('formatHeadcount: past complete', () => {
    const row = { start_count: 24, end_count: 25, end_count_is_live: false } as ClassHistoryRow
    expect(formatHeadcount(row)).toBe('24 → 25')
  })

  it('formatHeadcount: no data', () => {
    const row = { start_count: null, end_count: null, end_count_is_live: false } as ClassHistoryRow
    expect(formatHeadcount(row)).toBe('— 資料不足')
  })

  it('formatNetChange', () => {
    expect(formatNetChange(2)).toEqual({ text: '▲ +2', type: 'up' })
    expect(formatNetChange(-2)).toEqual({ text: '▼ -2', type: 'down' })
    expect(formatNetChange(0)).toEqual({ text: '±0', type: 'flat' })
    expect(formatNetChange(null)).toEqual({ text: '—', type: 'none' })
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-frontend/.claude/worktrees/employee-class-history && npx vitest run src/utils/__tests__/classHistory.spec.ts`
Expected: FAIL（找不到 `../classHistory`）

- [ ] **Step 3: 實作 helper**

Create `src/utils/classHistory.ts`：

```typescript
export interface ClassHistoryCoTeacher {
  role: 'head' | 'assistant' | 'art'
  employee_id: number
  name: string
}

export interface ClassHistoryRow {
  school_year: number
  semester: number
  classroom_id: number
  classroom_name: string
  grade_name: string | null
  role: 'head' | 'assistant'
  co_teachers: ClassHistoryCoTeacher[]
  is_current: boolean
  start_count: number | null
  end_count: number | null
  end_count_is_live: boolean
  net_change: number | null
}

const ROLE_LABELS: Record<string, string> = {
  head: '導師',
  assistant: '助教',
  art: '才藝',
}

export const roleLabel = (role: string): string => ROLE_LABELS[role] ?? role

export const formatSemester = (schoolYear: number, semester: number): string =>
  `${schoolYear} ${semester === 1 ? '上學期' : '下學期'}`

export const formatCoTeachers = (cos: ClassHistoryCoTeacher[]): string => {
  if (!cos.length) return '—'
  return cos.map(c => `${roleLabel(c.role)} ${c.name}`).join(' · ')
}

export const formatHeadcount = (row: ClassHistoryRow): string => {
  const { start_count, end_count, end_count_is_live } = row
  if (start_count == null && end_count == null) return '— 資料不足'
  const left = start_count == null ? '—' : String(start_count)
  const right =
    end_count == null
      ? '—'
      : end_count_is_live
        ? `目前 ${end_count}`
        : String(end_count)
  return `${left} → ${right}`
}

export type NetChangeType = 'up' | 'down' | 'flat' | 'none'

export const formatNetChange = (
  net: number | null,
): { text: string; type: NetChangeType } => {
  if (net == null) return { text: '—', type: 'none' }
  if (net > 0) return { text: `▲ +${net}`, type: 'up' }
  if (net < 0) return { text: `▼ ${net}`, type: 'down' }
  return { text: '±0', type: 'flat' }
}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-frontend/.claude/worktrees/employee-class-history && npx vitest run src/utils/__tests__/classHistory.spec.ts`
Expected: 全 passed

- [ ] **Step 5: Commit**

```bash
git add src/utils/classHistory.ts src/utils/__tests__/classHistory.spec.ts
git commit -m "feat(employees): 班級歷程格式化 helper + 測試"
```

---

### Task 8: EmployeeView.vue 分頁

**Files:**
- Modify: `src/views/EmployeeView.vue`

- [ ] **Step 1: import helper 與 api**

在 `<script setup lang="ts">` 區，於既有 `import { listEmployeeEducations, ... } from '@/api/employees'` 加入 `listEmployeeClassHistory`（路徑沿用該檔既有 employees api import 寫法）。新增：

```typescript
import {
  formatSemester,
  roleLabel,
  formatCoTeachers,
  formatHeadcount,
  formatNetChange,
  type ClassHistoryRow,
} from '@/utils/classHistory'
```

- [ ] **Step 2: 新增 ref 與 fetch（接在 `const contracts = ref...` 與 `fetchContracts` 附近）**

```typescript
const classHistory = ref<ClassHistoryRow[]>([])

const fetchClassHistory = async () => {
  if (!currentDetail.value.id) return
  const res = await listEmployeeClassHistory(currentDetail.value.id as number)
  classHistory.value = (res.data.rows ?? []) as ClassHistoryRow[]
}
```

- [ ] **Step 3: 接上 `onDetailTabChange`**

在 `else if (name === 'attendance') await fetchAttendance()` 之後加：

```typescript
    else if (name === 'classHistory') await fetchClassHistory()
```

- [ ] **Step 4: `handleDetail` 重置區清空**

在 `attendanceRecords.value = []` 之後加：

```typescript
    classHistory.value = []
```

- [ ] **Step 5: 在 `<el-tabs>` 內、出勤分頁 `</el-tab-pane>` 之後、`</el-tabs>` 之前新增分頁**

```html
            <!-- 班級歷程 -->
            <el-tab-pane label="班級歷程" name="classHistory">
              <el-table v-if="classHistory.length" :data="classHistory" style="width: 100%;">
                <el-table-column label="學年 / 學期" width="150">
                  <template #default="scope">
                    {{ formatSemester(scope.row.school_year, scope.row.semester) }}
                    <el-tag v-if="scope.row.is_current" type="success" size="small">現在</el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="班級（年級）">
                  <template #default="scope">
                    {{ scope.row.classroom_name }}<span v-if="scope.row.grade_name">（{{ scope.row.grade_name }}）</span>
                  </template>
                </el-table-column>
                <el-table-column label="角色" width="90">
                  <template #default="scope">
                    <el-tag :type="scope.row.role === 'head' ? 'primary' : 'warning'" size="small">
                      {{ roleLabel(scope.row.role) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="同班搭檔">
                  <template #default="scope">{{ formatCoTeachers(scope.row.co_teachers) }}</template>
                </el-table-column>
                <el-table-column label="期初 → 期末" width="140">
                  <template #default="scope">{{ formatHeadcount(scope.row) }}</template>
                </el-table-column>
                <el-table-column label="淨變化" width="100">
                  <template #default="scope">
                    <span :class="`net-${formatNetChange(scope.row.net_change).type}`">
                      {{ formatNetChange(scope.row.net_change).text }}
                    </span>
                  </template>
                </el-table-column>
              </el-table>
              <el-empty v-else description="尚無帶班紀錄" />
            </el-tab-pane>
```

- [ ] **Step 6: 加樣式（在該檔 `<style scoped>` 內）**

```css
.net-up { color: var(--el-color-success); }
.net-down { color: var(--el-color-danger); }
.net-flat,
.net-none { color: var(--el-text-color-secondary); }
```

- [ ] **Step 7: typecheck + 既有測試**

Run: `cd ~/Desktop/ivy-frontend/.claude/worktrees/employee-class-history && npm run typecheck && npx vitest run src/utils/__tests__/classHistory.spec.ts`
Expected: typecheck exit 0；vitest passed

- [ ] **Step 8: Commit**

```bash
git add src/views/EmployeeView.vue
git commit -m "feat(employees): 員工詳情新增班級歷程分頁"
```

---

## 整合驗證與收尾

- [ ] **整合測試**：`cd ~/Desktop/ivyManageSystem && ./start.sh`，登入 admin → 員工管理 → 開一位有帶班的員工詳情 → 點「班級歷程」分頁，確認列出學期/班級/角色/搭檔/人數；對沒帶過班的員工顯示空狀態。
- [ ] **漂移檢查**：`cd ~/Desktop/ivy-frontend/.claude/worktrees/employee-class-history && npm run gen:api:check`（無漂移）。
- [ ] **收尾**：依 workspace §收尾紀律 —— 後端先 push（注意後端 Zeabur SUSPENDED，push 不部署/不跑 migration；本功能無 migration）→ 前端 push（RUNNING，push 即上線）→ 確認兩 repo CI 綠 → `git worktree remove` 兩個 worktree、刪分支。

---

## 自我檢查紀錄（writing-plans self-review）

- **Spec 覆蓋**：§4 後端端點/schema/查詢/人數 → Task 1-4；§5 前端薄殼 → Task 5-8；§6 不做回放 → `_term_headcounts` 不使用 transfer（已落實）；§7 範圍外 finding → 不在計畫內（正確）。
- **Placeholder 掃描**：無 TBD/TODO；每個 code step 皆有完整程式碼。
- **型別一致**：後端 dict key 與 `ClassHistoryRow` 欄位逐一對齊（start_count/end_count/end_count_is_live/net_change/co_teachers）；前端 `ClassHistoryRow` interface 與後端 schema 同名同型；`formatNetChange` 回傳型別 `{text, type}` 在 vitest 與 Vue 模板一致使用。
- **已知前置依賴**：Task 5 需後端端點先就緒（同 session 先做完後端 Task 1-4）。
