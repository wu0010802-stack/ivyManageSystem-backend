# 後台全域搜尋（完整版）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把後台 Ctrl+K 全域搜尋從「4 類前端拼湊」升級為「8 類實體 + 頁面導航、後端逐類權限把關、點擊跳到該筆」的完整搜尋。

**Architecture:** 新增一支後端 `GET /api/search?q=`（仿 `api/portal/search.py`），由小 helper 函式各查一類實體、各自做 READ 權限把關、PII 遮罩、寫 READ 稽核；前端重寫 `GlobalSearch.vue` 打這支 API，頁面清單改從 router `meta.title` 動態產生，點擊依類型跳轉（學生→檔案頁，其餘→列表頁帶關鍵字）。

**Tech Stack:** FastAPI + SQLAlchemy + PostgreSQL（測試走 SQLite）；Vue 3 `<script setup lang="ts">` + Element Plus + axios；OpenAPI→TS codegen。

**對應 spec:** `docs/superpowers/specs/2026-06-05-admin-global-search-design.md`

**分支（off origin/main）:** 後端 `feat/admin-global-search-2026-06-05-be`、前端 `feat/admin-global-search-2026-06-05-fe`。前後端分開 commit。

---

## Phase A — 後端 `/api/search` endpoint

> 全部在後端 repo `~/Desktop/ivy-backend`。測試 fixture **完全比照** `tests/test_employee_docs.py` 的 `client_with_db`（自建 app + swap `base_module._engine`/`_SessionFactory` 到 SQLite temp + `Base.metadata.create_all` + 用 `auth_router` 登入拿 token）。每個測試建立帶特定 `permission_names` 的 `User` + 對應資料，登入後打 `GET /api/search`。

### Task A1: 建立 endpoint 骨架（守衛 + 字元門檻 + 空回應 + response_model + 註冊）

**Files:**
- Create: `api/search.py`
- Modify: `main.py`（import + `app.include_router`）
- Test: `tests/test_search.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_search.py
"""後台全域搜尋 /api/search 測試。"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.search import router as search_router
from models.database import Base, Employee, User
from utils.auth import hash_password


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "search.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_engine, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(search_router)
    try:
        with TestClient(app) as client:
            yield client, session_factory
    finally:
        base_module._engine = old_engine
        base_module._SessionFactory = old_sf


def _make_user(session_factory, *, username, role, permission_names):
    s = session_factory()
    try:
        emp = Employee(employee_id=f"E-{username}", name=username, is_active=True)
        s.add(emp)
        s.flush()
        u = User(
            username=username,
            password_hash=hash_password("pw123456"),
            role=role,
            employee_id=emp.id,
            permission_names=permission_names,
            is_active=True,
        )
        s.add(u)
        s.commit()
        return emp.id
    finally:
        s.close()


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": "pw123456"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ⚠ 落地前先對照範本：以上 `_make_user` 的 `User(...)` 欄位名（`password_hash`
# vs `hashed_password`、`permission_names` 的型別/預設）、登入路徑（`/api/auth/login`）
# 與回傳 token 欄位名（`access_token`）皆**以既有測試為準**。請打開
# `tests/test_employee_docs.py` 看它怎麼建 User + 登入拿 token，照抄正確欄位/路徑後
# 再修正此處。`User` 定義見 `models/database.py`（`grep -n "class User" models/database.py`）。


def test_short_query_returns_empty(client_with_db):
    client, sf = client_with_db
    _make_user(sf, username="admin1", role="admin", permission_names=["*"])
    headers = _login(client, "admin1")
    r = client.get("/api/search", params={"q": "a"}, headers=headers)  # < 2 字
    assert r.status_code == 200
    body = r.json()
    assert body["q"] == "a"
    assert body["students"] == [] and body["employees"] == []


def test_teacher_and_parent_are_forbidden(client_with_db):
    client, sf = client_with_db
    _make_user(sf, username="teach1", role="teacher", permission_names=["STUDENTS_READ:own_class"])
    headers = _login(client, "teach1")
    r = client.get("/api/search", params={"q": "abc"}, headers=headers)
    assert r.status_code == 403
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_search.py -q`
Expected: FAIL（`api.search` 不存在 / ImportError）

- [ ] **Step 3: 建立 endpoint 骨架**

```python
# api/search.py
"""後台全域搜尋 endpoint。

GET /api/search?q=xxx → 一次回 8 類 entity 各 ≤ 8 筆。

權限：staff-only（拒絕 parent / teacher，teacher 走 api/portal/search）。
逐類做 READ 權限把關——無對應 READ 權限的類別回空陣列。
回傳含跨人 PII（家長遮罩電話、學生/家長姓名），比照 portal/search 顯式寫 READ audit。
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import or_

from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["search"])

SECTION_LIMIT = 8
MIN_QUERY_LEN = 2


class SearchStudentItem(BaseModel):
    id: int
    name: str
    student_id: Optional[str] = None
    classroom_name: str = ""


class SearchEmployeeItem(BaseModel):
    id: int
    name: str
    employee_id: Optional[str] = None
    title: str = ""


class SearchGuardianItem(BaseModel):
    id: int
    name: str
    phone_masked: str = ""
    child_name: str = ""
    student_id: int


class SearchClassroomItem(BaseModel):
    id: int
    name: str
    school_year: Optional[int] = None
    semester: Optional[int] = None


class SearchFeeItem(BaseModel):
    record_id: int
    student_name: str
    classroom_name: str = ""
    period: str = ""
    status: str = ""


class SearchActivityItem(BaseModel):
    id: int
    student_name: str
    class_name: str = ""
    match_status: str = ""


class SearchRecruitmentItem(BaseModel):
    id: int
    child_name: str
    target_school_year: Optional[int] = None
    enrolled: bool = False


class SearchAnnouncementItem(BaseModel):
    id: int
    title: str
    created_at: Optional[str] = None


class GlobalSearchResult(BaseModel):
    q: str
    students: List[SearchStudentItem] = []
    employees: List[SearchEmployeeItem] = []
    guardians: List[SearchGuardianItem] = []
    classrooms: List[SearchClassroomItem] = []
    fees: List[SearchFeeItem] = []
    activity_registrations: List[SearchActivityItem] = []
    recruitment: List[SearchRecruitmentItem] = []
    announcements: List[SearchAnnouncementItem] = []


@router.get("/search", response_model=GlobalSearchResult)
def global_search(
    request: Request,
    q: str = Query(..., min_length=0, max_length=100),
    current_user: dict = Depends(get_current_user),
):
    role = current_user.get("role")
    if role in ("parent", "teacher"):
        raise HTTPException(status_code=403, detail="此搜尋僅供後台管理端使用")

    q_stripped = (q or "").strip()
    if len(q_stripped) < MIN_QUERY_LEN:
        return GlobalSearchResult(q=q)

    # Phase A 後續 task 會在此填入各 section 查詢
    return GlobalSearchResult(q=q)
```

- [ ] **Step 4: 註冊 router**

在 `main.py` 既有 `from api.employees import router as employees_router` 附近加：
```python
from api.search import router as search_router
```
在 `app.include_router(employees_router)` 附近加（search 已自帶 `/api` prefix，不要再加 prefix=）：
```python
app.include_router(search_router)
```

- [ ] **Step 5: 跑測試確認通過**

Run: `python -m pytest tests/test_search.py -q`
Expected: PASS（2 passed）

- [ ] **Step 6: Commit**

```bash
git add api/search.py main.py tests/test_search.py
git commit -m "feat(search): 後台全域搜尋 endpoint 骨架（守衛+門檻+schema）"
```

---

### Task A2: 學生 section（含 own_class scope）

**Files:**
- Modify: `api/search.py`
- Test: `tests/test_search.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_students_section_and_permission_gate(client_with_db):
    client, sf = client_with_db
    # 建班級 + 兩個學生
    from models.database import Classroom, Student
    s = sf()
    cr = Classroom(name="向日葵班", school_year=114, semester=1, is_active=True)
    s.add(cr); s.flush()
    s.add(Student(name="王小明", student_id="S001", classroom_id=cr.id,
                  is_active=True, lifecycle_status="active"))
    s.add(Student(name="李大華", student_id="S002", classroom_id=cr.id,
                  is_active=True, lifecycle_status="active"))
    s.commit(); s.close()

    # 有 STUDENTS_READ → 搜得到
    _make_user(sf, username="reader", role="supervisor", permission_names=["STUDENTS_READ"])
    h = _login(client, "reader")
    r = client.get("/api/search", params={"q": "王小"}, headers=h)
    assert r.status_code == 200
    names = [x["name"] for x in r.json()["students"]]
    assert "王小明" in names and "李大華" not in names

    # 無 STUDENTS_READ → 學生區塊空
    _make_user(sf, username="noread", role="accountant", permission_names=["FEES_READ"])
    h2 = _login(client, "noread")
    r2 = client.get("/api/search", params={"q": "王小"}, headers=h2)
    assert r2.json()["students"] == []
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_search.py::test_students_section_and_permission_gate -q`
Expected: FAIL（students 仍為 []）

- [ ] **Step 3: 實作學生 helper + 接線**

在 `api/search.py` 頂部 import 區補：
```python
from models.classroom import (
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
)
from models.database import Classroom, Student, get_session
from utils.permissions import Permission, has_permission
from utils.portfolio_access import accessible_classroom_ids, is_unrestricted

_TERMINAL = [LIFECYCLE_GRADUATED, LIFECYCLE_WITHDRAWN, LIFECYCLE_TRANSFERRED]
```

加 helper：
```python
def _search_students(session, pattern: str, current_user: dict) -> list[dict]:
    code = Permission.STUDENTS_READ.value
    unrestricted = is_unrestricted(current_user, code=code)
    qy = session.query(Student).filter(
        Student.is_active.is_(True),
        Student.lifecycle_status.notin_(_TERMINAL),
        or_(Student.name.ilike(pattern), Student.student_id.ilike(pattern)),
    )
    if not unrestricted:
        scope = accessible_classroom_ids(session, current_user, code=code)
        if not scope:
            return []
        qy = qy.filter(Student.classroom_id.in_(scope))
    rows = qy.order_by(Student.name.asc()).limit(SECTION_LIMIT).all()
    cr_map: dict[int, str] = {}
    cids = {r.classroom_id for r in rows if r.classroom_id}
    if cids:
        cr_map = {
            cid: name
            for cid, name in session.query(Classroom.id, Classroom.name)
            .filter(Classroom.id.in_(cids))
            .all()
        }
    return [
        {
            "id": r.id,
            "name": r.name,
            "student_id": r.student_id,
            "classroom_name": cr_map.get(r.classroom_id, ""),
        }
        for r in rows
    ]
```

把 endpoint 改成查 DB 並接學生 section：
```python
    pattern = f"%{q_stripped}%"
    perms = current_user.get("permission_names")
    session = get_session()
    try:
        students = (
            _search_students(session, pattern, current_user)
            if has_permission(perms, Permission.STUDENTS_READ)
            else []
        )
        return GlobalSearchResult(q=q, students=students)
    finally:
        session.close()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_search.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add api/search.py tests/test_search.py
git commit -m "feat(search): 學生 section（含 own_class scope + 權限把關）"
```

---

### Task A3: 員工 section

**Files:** Modify `api/search.py`；Test `tests/test_search.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_employees_section(client_with_db):
    client, sf = client_with_db
    from models.database import Employee
    s = sf()
    s.add(Employee(employee_id="E100", name="陳老師", is_active=True, title="教師"))
    s.add(Employee(employee_id="E200", name="林主任", is_active=False))  # 離職不出現
    s.commit(); s.close()
    _make_user(sf, username="hr1", role="hr", permission_names=["EMPLOYEES_READ"])
    h = _login(client, "hr1")
    r = client.get("/api/search", params={"q": "陳"}, headers=h)
    rows = r.json()["employees"]
    assert any(x["name"] == "陳老師" and x["title"] == "教師" for x in rows)
    # 無 EMPLOYEES_READ → 空
    _make_user(sf, username="hr0", role="supervisor", permission_names=["STUDENTS_READ"])
    h0 = _login(client, "hr0")
    assert client.get("/api/search", params={"q": "陳"}, headers=h0).json()["employees"] == []
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_search.py::test_employees_section -q`
Expected: FAIL

- [ ] **Step 3: 實作**

import 區補 `from models.employee import Employee`（若上方未 import）。加 helper：
```python
def _search_employees(session, pattern: str) -> list[dict]:
    rows = (
        session.query(Employee)
        .filter(
            Employee.is_active.is_(True),
            or_(Employee.name.ilike(pattern), Employee.employee_id.ilike(pattern)),
        )
        .order_by(Employee.name.asc())
        .limit(SECTION_LIMIT)
        .all()
    )
    return [
        {"id": e.id, "name": e.name, "employee_id": e.employee_id, "title": e.title or ""}
        for e in rows
    ]
```
endpoint 內 `return` 前接線：
```python
        employees = (
            _search_employees(session, pattern)
            if has_permission(perms, Permission.EMPLOYEES_READ)
            else []
        )
```
並把 `return GlobalSearchResult(q=q, students=students, employees=employees)`。

> 註：`Employee.title` 為 legacy 字串職稱欄位（`models/employee.py:64`）。本功能用它當副標即可；若日後要顯示 `job_title_id` 對應的正式職稱，再 batch-join JobTitle（YAGNI，現不做）。

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_search.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add api/search.py tests/test_search.py
git commit -m "feat(search): 員工 section"
```

---

### Task A4: 家長 section（PII 遮罩 + scope）

**Files:** Modify `api/search.py`；Test `tests/test_search.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_guardians_section_masks_phone(client_with_db):
    client, sf = client_with_db
    from models.database import Classroom, Student, Guardian
    s = sf()
    cr = Classroom(name="A班", school_year=114, semester=1, is_active=True); s.add(cr); s.flush()
    stu = Student(name="王小明", student_id="S001", classroom_id=cr.id,
                  is_active=True, lifecycle_status="active"); s.add(stu); s.flush()
    s.add(Guardian(student_id=stu.id, name="王大華", phone="0912345678", is_primary=True))
    s.commit(); s.close()
    _make_user(sf, username="sup1", role="supervisor", permission_names=["GUARDIANS_READ"])
    h = _login(client, "sup1")
    r = client.get("/api/search", params={"q": "王大"}, headers=h)
    rows = r.json()["guardians"]
    assert rows and rows[0]["name"] == "王大華"
    assert rows[0]["child_name"] == "王小明" and rows[0]["student_id"] == stu.id
    assert "0912345678" not in rows[0]["phone_masked"]  # 已遮罩
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_search.py::test_guardians_section_masks_phone -q`
Expected: FAIL

- [ ] **Step 3: 實作**

import 區補 `from models.database import Guardian`（併入既有 import）與 `from utils.masking import mask_phone`。加 helper：
```python
def _search_guardians(session, pattern: str, current_user: dict) -> list[dict]:
    code = Permission.GUARDIANS_READ.value
    unrestricted = is_unrestricted(current_user, code=code)
    qy = (
        session.query(Guardian, Student)
        .join(Student, Guardian.student_id == Student.id)
        .filter(
            Student.is_active.is_(True),
            Student.lifecycle_status.notin_(_TERMINAL),
            or_(Guardian.name.ilike(pattern), Guardian.phone.ilike(pattern)),
        )
    )
    if not unrestricted:
        scope = accessible_classroom_ids(session, current_user, code=code)
        if not scope:
            return []
        qy = qy.filter(Student.classroom_id.in_(scope))
    rows = qy.order_by(Guardian.name.asc()).limit(SECTION_LIMIT).all()
    return [
        {
            "id": g.id,
            "name": g.name,
            "phone_masked": mask_phone(g.phone) or "",
            "child_name": stu.name,
            "student_id": stu.id,
        }
        for g, stu in rows
    ]
```
endpoint 接線：
```python
        guardians = (
            _search_guardians(session, pattern, current_user)
            if has_permission(perms, Permission.GUARDIANS_READ)
            else []
        )
```
加入 `GlobalSearchResult(..., guardians=guardians)`。

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_search.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add api/search.py tests/test_search.py
git commit -m "feat(search): 家長 section（電話遮罩 + scope）"
```

---

### Task A5: 班級 + 公告 section

**Files:** Modify `api/search.py`；Test `tests/test_search.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_classrooms_and_announcements_sections(client_with_db):
    client, sf = client_with_db
    from models.database import Classroom
    from models.event import Announcement
    s = sf()
    s.add(Classroom(name="彩虹班", school_year=114, semester=1, is_active=True))
    s.add(Announcement(title="彩虹班親師座談", content="內容"))
    s.commit(); s.close()
    _make_user(sf, username="adm", role="admin", permission_names=["*"])
    h = _login(client, "adm")
    r = client.get("/api/search", params={"q": "彩虹"}, headers=h)
    body = r.json()
    assert any(c["name"] == "彩虹班" for c in body["classrooms"])
    assert any(a["title"] == "彩虹班親師座談" for a in body["announcements"])
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_search.py::test_classrooms_and_announcements_sections -q`
Expected: FAIL

- [ ] **Step 3: 實作**

import 區補 `from models.event import Announcement`。加 helpers：
```python
def _search_classrooms(session, pattern: str) -> list[dict]:
    rows = (
        session.query(Classroom)
        .filter(Classroom.is_active.is_(True), Classroom.name.ilike(pattern))
        .order_by(Classroom.school_year.desc(), Classroom.name.asc())
        .limit(SECTION_LIMIT)
        .all()
    )
    return [
        {"id": c.id, "name": c.name, "school_year": c.school_year, "semester": c.semester}
        for c in rows
    ]


def _search_announcements(session, pattern: str) -> list[dict]:
    rows = (
        session.query(Announcement)
        .filter(or_(Announcement.title.ilike(pattern), Announcement.content.ilike(pattern)))
        .order_by(Announcement.created_at.desc())
        .limit(SECTION_LIMIT)
        .all()
    )
    return [
        {
            "id": a.id,
            "title": a.title,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]
```
endpoint 接線：
```python
        classrooms = (
            _search_classrooms(session, pattern)
            if has_permission(perms, Permission.CLASSROOMS_READ)
            else []
        )
        announcements = (
            _search_announcements(session, pattern)
            if has_permission(perms, Permission.ANNOUNCEMENTS_READ)
            else []
        )
```
併入 `GlobalSearchResult(..., classrooms=classrooms, announcements=announcements)`。

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_search.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: Commit**

```bash
git add api/search.py tests/test_search.py
git commit -m "feat(search): 班級 + 公告 section"
```

---

### Task A6: 學費 + 才藝報名 + 招生 section

**Files:** Modify `api/search.py`；Test `tests/test_search.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_fees_activity_recruitment_sections(client_with_db):
    client, sf = client_with_db
    from models.fees import StudentFeeRecord
    from models.activity import ActivityRegistration
    from models.recruitment import RecruitmentVisit
    s = sf()
    s.add(StudentFeeRecord(student_id=1, student_name="趙小妹", classroom_name="A班",
                           fee_item_name="月費", period="114-1", status="unpaid",
                           amount_due=5000))
    s.add(ActivityRegistration(student_name="趙小妹", class_name="A班",
                               parent_phone="0911222333", is_active=True,
                               match_status="matched"))
    s.add(RecruitmentVisit(child_name="趙小寶", target_school_year=115, enrolled=False))
    s.commit(); s.close()
    _make_user(sf, username="adm", role="admin", permission_names=["*"])
    h = _login(client, "adm")
    rf = client.get("/api/search", params={"q": "趙小妹"}, headers=h).json()
    assert any(x["student_name"] == "趙小妹" for x in rf["fees"])
    assert any(x["student_name"] == "趙小妹" for x in rf["activity_registrations"])
    rr = client.get("/api/search", params={"q": "趙小寶"}, headers=h).json()
    assert any(x["child_name"] == "趙小寶" for x in rr["recruitment"])
```

> 註：`StudentFeeRecord` / `ActivityRegistration` 的必填欄位以各 model 為準；若上面建構子缺必填欄位導致 IntegrityError，補上 model 要求的 NOT NULL 欄位（見 `models/fees.py`、`models/activity.py`）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_search.py::test_fees_activity_recruitment_sections -q`
Expected: FAIL

- [ ] **Step 3: 實作**

import 區補：
```python
from models.activity import ActivityRegistration
from models.fees import StudentFeeRecord
from models.recruitment import RecruitmentVisit
```
加 helpers：
```python
def _search_fees(session, pattern: str) -> list[dict]:
    rows = (
        session.query(StudentFeeRecord)
        .filter(
            or_(
                StudentFeeRecord.student_name.ilike(pattern),
                StudentFeeRecord.fee_item_name.ilike(pattern),
            )
        )
        .order_by(StudentFeeRecord.id.desc())
        .limit(SECTION_LIMIT)
        .all()
    )
    return [
        {
            "record_id": r.id,
            "student_name": r.student_name,
            "classroom_name": r.classroom_name or "",
            "period": r.period or "",
            "status": r.status or "",
        }
        for r in rows
    ]


def _search_activity(session, pattern: str) -> list[dict]:
    rows = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.is_active.is_(True),
            or_(
                ActivityRegistration.student_name.ilike(pattern),
                ActivityRegistration.class_name.ilike(pattern),
                ActivityRegistration.parent_phone.ilike(pattern),
            ),
        )
        .order_by(ActivityRegistration.id.desc())
        .limit(SECTION_LIMIT)
        .all()
    )
    return [
        {
            "id": r.id,
            "student_name": r.student_name,
            "class_name": r.class_name or "",
            "match_status": r.match_status or "",
        }
        for r in rows
    ]


def _search_recruitment(session, pattern: str) -> list[dict]:
    rows = (
        session.query(RecruitmentVisit)
        .filter(
            or_(
                RecruitmentVisit.child_name.ilike(pattern),
                RecruitmentVisit.address.ilike(pattern),
                RecruitmentVisit.notes.ilike(pattern),
                RecruitmentVisit.parent_response.ilike(pattern),
            )
        )
        .order_by(RecruitmentVisit.id.desc())
        .limit(SECTION_LIMIT)
        .all()
    )
    return [
        {
            "id": r.id,
            "child_name": r.child_name,
            "target_school_year": r.target_school_year,
            "enrolled": bool(r.enrolled),
        }
        for r in rows
    ]
```
endpoint 接線：
```python
        fees = (
            _search_fees(session, pattern)
            if has_permission(perms, Permission.FEES_READ)
            else []
        )
        activity_registrations = (
            _search_activity(session, pattern)
            if has_permission(perms, Permission.ACTIVITY_READ)
            else []
        )
        recruitment = (
            _search_recruitment(session, pattern)
            if has_permission(perms, Permission.RECRUITMENT_READ)
            else []
        )
```
併入最終 `GlobalSearchResult(...)`（含全部 8 類）。

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_search.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: Commit**

```bash
git add api/search.py tests/test_search.py
git commit -m "feat(search): 學費 + 才藝報名 + 招生 section"
```

---

### Task A7: 顯式稽核（READ audit）

**Files:** Modify `api/search.py`；Test `tests/test_search.py`

- [ ] **Step 1: 寫失敗測試**

```python
def test_search_writes_read_audit(client_with_db):
    client, sf = client_with_db
    _make_user(sf, username="adm", role="admin", permission_names=["*"])
    h = _login(client, "adm")
    r = client.get("/api/search", params={"q": "測試"}, headers=h)
    assert r.status_code == 200
    # 查 audit 表是否有一筆 admin_global_search READ
    from models.database import AuditLog  # 若名稱不同，依 utils/audit.py 寫入的 model 調整
    s = sf()
    try:
        rows = s.query(AuditLog).filter(AuditLog.entity_type == "admin_global_search").all()
        assert len(rows) == 1
        assert rows[0].action == "READ"
    finally:
        s.close()
```

> 註：`AuditLog` model 名稱/欄位以 `utils/audit.py` `write_explicit_audit` 實際寫入者為準（先 `grep -n "class .*Audit" models/` 確認 import 路徑與欄位名 `entity_type`/`action`）。若欄位名不同，調整斷言。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_search.py::test_search_writes_read_audit -q`
Expected: FAIL（無 audit row）

- [ ] **Step 3: 實作**

import 區補 `from utils.audit import write_explicit_audit`。在 endpoint 組好 `result` 後、`return` 前加（比照 `api/portal/search.py:335-350`）：
```python
        result = GlobalSearchResult(
            q=q,
            students=students,
            employees=employees,
            guardians=guardians,
            classrooms=classrooms,
            fees=fees,
            activity_registrations=activity_registrations,
            recruitment=recruitment,
            announcements=announcements,
        )
        write_explicit_audit(
            request,
            action="READ",
            entity_type="admin_global_search",
            summary=f"後台全域搜尋（q={q_stripped[:32]}）",
            changes={
                "q": q_stripped[:64],
                "result_counts": {
                    "students": len(students),
                    "employees": len(employees),
                    "guardians": len(guardians),
                    "classrooms": len(classrooms),
                    "fees": len(fees),
                    "activity_registrations": len(activity_registrations),
                    "recruitment": len(recruitment),
                    "announcements": len(announcements),
                },
            },
        )
        return result
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_search.py -q`
Expected: PASS（8 passed）

- [ ] **Step 5: 跑全套件 sanity（搜尋相關）**

Run: `python -m pytest tests/test_search.py -v`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add api/search.py tests/test_search.py
git commit -m "feat(search): 顯式 READ 稽核（admin_global_search）"
```

---

## Phase B — OpenAPI codegen（前端型別）

### Task B1: 產 openapi.json + 前端 schema.d.ts

**Files:**
- 後端：執行 `scripts/dump_openapi.py`（產 local-only `openapi.json`，不 commit）
- 前端：`src/api/_generated/schema.d.ts`（commit）

- [ ] **Step 1: 後端產 openapi**

Run: `cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py`
Expected: 產出 `openapi.json`（含 `/search` path、`GlobalSearchResult` schema）

- [ ] **Step 2: 前端 regen 型別**

Run: `cd ~/Desktop/ivy-frontend && npm run gen:api`
Expected: `src/api/_generated/schema.d.ts` 出現 `/search` path key + 各 `Search*Item` schema

- [ ] **Step 3: 漂移檢查**

Run: `npm run gen:api:check`
Expected: 乾淨（除了 schema.d.ts 本身的新增）

- [ ] **Step 4: Commit（前端分支）**

```bash
cd ~/Desktop/ivy-frontend
git add src/api/_generated/schema.d.ts
git commit -m "chore(api): regen schema.d.ts 納入 /search endpoint"
```

> `openapi.json` 是 dev-time artifact，`.gitignore` 已擋，**不要** commit。

---

## Phase C — 前端搜尋面板

> 全部在 `~/Desktop/ivy-frontend`，前端分支。

### Task C1: api wrapper `src/api/search.ts`

**Files:** Create `src/api/search.ts`

- [ ] **Step 1: 建立 api wrapper**

```ts
// src/api/search.ts
import api from './index'
import type { AxiosResp } from './_generated/typed'

export const globalSearch = (q: string): AxiosResp<'/search', 'get'> =>
    api.get('/search', { params: { q } })
```

- [ ] **Step 2: typecheck**

Run: `cd ~/Desktop/ivy-frontend && npm run typecheck`
Expected: 0 error（若 `/search` 型別存在則通過；否則回頭確認 Task B1）

- [ ] **Step 3: Commit**

```bash
git add src/api/search.ts
git commit -m "feat(search): 前端 globalSearch api wrapper"
```

---

### Task C2: 重寫 `GlobalSearch.vue`

**Files:**
- Modify: `src/components/GlobalSearch.vue`（整段 `<template>` 與 `<script setup>` 重寫；`<style>` 兩塊**保留不動**）
- Test: `src/components/__tests__/GlobalSearch.spec.ts`

- [ ] **Step 1: 寫失敗測試**

```ts
// src/components/__tests__/GlobalSearch.spec.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import GlobalSearch from '../GlobalSearch.vue'

const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, getRoutes: () => [] }),
}))
vi.mock('@/utils/auth', () => ({ canAccessRoute: () => true }))
vi.mock('@/utils/highlight', () => ({ highlight: (s: string) => s }))
const globalSearch = vi.fn()
vi.mock('@/api/search', () => ({ globalSearch: (q: string) => globalSearch(q) }))

describe('GlobalSearch', () => {
  beforeEach(() => { push.mockClear(); globalSearch.mockReset() })

  it('短於 2 字不呼叫 API', async () => {
    const wrapper = mount(GlobalSearch)
    ;(wrapper.vm as any).open()
    await wrapper.find('input').setValue('a')
    await new Promise(r => setTimeout(r, 350))
    expect(globalSearch).not.toHaveBeenCalled()
  })

  it('≥ 2 字呼叫 API 並渲染學生區塊 + 點擊跳檔案頁', async () => {
    globalSearch.mockResolvedValue({ data: {
      q: '王', students: [{ id: 7, name: '王小明', student_id: 'S1', classroom_name: 'A班' }],
      employees: [], guardians: [], classrooms: [], fees: [],
      activity_registrations: [], recruitment: [], announcements: [],
    }})
    const wrapper = mount(GlobalSearch)
    ;(wrapper.vm as any).open()
    await wrapper.find('input').setValue('王')
    await new Promise(r => setTimeout(r, 350))
    await flushPromises()
    expect(globalSearch).toHaveBeenCalledWith('王')
    expect(wrapper.text()).toContain('王小明')
    await wrapper.find('.gs-item').trigger('click')
    expect(push).toHaveBeenCalledWith('/students/profile/7')
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/components/__tests__/GlobalSearch.spec.ts`
Expected: FAIL（舊版無此行為 / 結構不符）

- [ ] **Step 3: 重寫 `<template>` + `<script setup>`**

把 `GlobalSearch.vue` 的 `<template>` 與 `<script setup lang="ts">` 整段換成下方內容（**`<style scoped>` 與末段全域 `<style>` 兩塊保留原檔不動**）：

```vue
<template>
  <Teleport to="body">
    <Transition name="gs-fade">
      <div v-if="visible" class="gs-overlay" @click.self="close">
        <div class="gs-modal" role="dialog" aria-modal="true" aria-label="全局搜尋">
          <div class="gs-input-wrap">
            <el-icon class="gs-input-icon"><Search /></el-icon>
            <input
              ref="inputRef"
              v-model="query"
              class="gs-input"
              placeholder="搜尋學生、員工、家長、班級、學費、才藝、招生、公告、頁面…"
              autocomplete="off"
              @keydown="onKeydown"
            />
            <span v-if="isLoading" class="gs-spinner"></span>
            <kbd class="gs-esc-hint" @click="close">esc</kbd>
          </div>

          <div class="gs-results" ref="resultsRef">
            <template v-if="query.trim().length >= 2">
              <template v-for="group in groups" :key="group.key">
                <div class="gs-section-title">{{ group.title }}</div>
                <div
                  v-for="entry in group.items"
                  :key="group.key + '-' + entry.flatIndex"
                  class="gs-item"
                  :class="{ 'gs-item--active': activeIndex === entry.flatIndex }"
                  @mouseenter="activeIndex = entry.flatIndex"
                  @click="selectByFlat(entry.flatIndex)"
                >
                  <el-icon class="gs-item-icon"><component :is="group.icon" /></el-icon>
                  <span class="gs-item-label" v-html="highlight(entry.label, query)"></span>
                  <span class="gs-item-sub">{{ entry.sub }}</span>
                </div>
              </template>

              <template v-if="pageEntries.length">
                <div class="gs-section-title">頁面</div>
                <div
                  v-for="entry in pageEntries"
                  :key="'page-' + entry.flatIndex"
                  class="gs-item"
                  :class="{ 'gs-item--active': activeIndex === entry.flatIndex }"
                  @mouseenter="activeIndex = entry.flatIndex"
                  @click="selectByFlat(entry.flatIndex)"
                >
                  <el-icon class="gs-item-icon"><Grid /></el-icon>
                  <span class="gs-item-label" v-html="highlight(entry.label, query)"></span>
                  <span class="gs-item-sub">{{ entry.sub }}</span>
                </div>
              </template>

              <div v-if="!groups.length && !pageEntries.length && !isLoading" class="gs-empty">
                無符合「{{ query }}」的結果
              </div>
            </template>
            <div v-else class="gs-hint">輸入至少 2 個字搜尋</div>
          </div>

          <div class="gs-footer">
            <span><kbd>↑</kbd><kbd>↓</kbd> 導航</span>
            <span><kbd>Enter</kbd> 選擇</span>
            <span><kbd>Esc</kbd> 關閉</span>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted, markRaw } from 'vue'
import { useRouter } from 'vue-router'
import { Search, User, Avatar, Bell, Grid } from '@element-plus/icons-vue'
import { globalSearch } from '@/api/search'
import { canAccessRoute } from '@/utils/auth'
import { highlight } from '@/utils/highlight'

const router = useRouter()

const visible = ref(false)
const query = ref('')
const activeIndex = ref(-1)
const isLoading = ref(false)
const inputRef = ref<HTMLInputElement | null>(null)
const resultsRef = ref<HTMLElement | null>(null)

type Item = Record<string, unknown>
const data = ref<Record<string, Item[]>>({})

interface SectionDef {
  key: string
  title: string
  icon: unknown
  label: (i: Item) => string
  sub: (i: Item) => string
  navigate: (i: Item) => void
}

const SECTIONS: SectionDef[] = [
  { key: 'students', title: '學生', icon: markRaw(Avatar),
    label: i => String(i.name ?? ''),
    sub: i => String(i.classroom_name || i.student_id || ''),
    navigate: i => router.push(`/students/profile/${i.id}`) },
  { key: 'employees', title: '員工', icon: markRaw(User),
    label: i => String(i.name ?? ''),
    sub: i => String(i.title || i.employee_id || ''),
    navigate: i => router.push({ path: '/employees', query: { section: 'employees', search: String(i.name ?? '') } }) },
  { key: 'guardians', title: '家長', icon: markRaw(Avatar),
    label: i => String(i.name ?? ''),
    sub: i => [i.child_name, i.phone_masked].filter(Boolean).join('．'),
    navigate: i => router.push(`/students/profile/${i.student_id}`) },
  { key: 'classrooms', title: '班級', icon: markRaw(Grid),
    label: i => String(i.name ?? ''),
    sub: i => i.school_year ? `${i.school_year} 學年` : '',
    navigate: () => router.push('/classrooms') },
  { key: 'fees', title: '學費', icon: markRaw(Grid),
    label: i => String(i.student_name ?? ''),
    sub: i => [i.period, i.status === 'paid' ? '已繳' : '未繳'].filter(Boolean).join('．'),
    navigate: i => router.push({ path: '/fees', query: { search: String(i.student_name ?? '') } }) },
  { key: 'activity_registrations', title: '才藝報名', icon: markRaw(Grid),
    label: i => String(i.student_name ?? ''),
    sub: i => String(i.class_name ?? ''),
    navigate: i => router.push({ path: '/activity/registrations', query: { search: String(i.student_name ?? '') } }) },
  { key: 'recruitment', title: '招生', icon: markRaw(Grid),
    label: i => String(i.child_name ?? ''),
    sub: i => i.target_school_year ? `${i.target_school_year} 學年` : '',
    navigate: i => router.push({ path: '/recruitment', query: { keyword: String(i.child_name ?? '') } }) },
  { key: 'announcements', title: '公告', icon: markRaw(Bell),
    label: i => String(i.title ?? ''),
    sub: () => '',
    navigate: () => router.push('/announcements') },
]

interface RenderedEntry { item: Item; flatIndex: number; label: string; sub: string }
interface RenderedGroup { key: string; title: string; icon: unknown; items: RenderedEntry[] }

const groups = computed<RenderedGroup[]>(() => {
  const out: RenderedGroup[] = []
  let idx = 0
  for (const sec of SECTIONS) {
    const rows = data.value[sec.key] || []
    const items = rows.map(item => ({ item, flatIndex: idx++, label: sec.label(item), sub: sec.sub(item) }))
    if (items.length) out.push({ key: sec.key, title: sec.title, icon: sec.icon, items })
  }
  return out
})

interface PageRow { title: string; path: string }
const pages = computed<PageRow[]>(() => {
  const q = query.value.trim()
  if (q.length < 2) return []
  const seen = new Set<string>()
  const out: PageRow[] = []
  for (const r of router.getRoutes()) {
    const title = r.meta?.title
    if (!title || r.path.includes(':') || seen.has(r.path)) continue
    if (!canAccessRoute(r.path)) continue
    if (!String(title).includes(q)) continue
    seen.add(r.path)
    out.push({ title: String(title), path: r.path })
  }
  return out.slice(0, 8)
})

const pageBase = computed(() => groups.value.reduce((n, g) => n + g.items.length, 0))
const pageEntries = computed<RenderedEntry[]>(() =>
  pages.value.map((p, i) => ({ item: p as unknown as Item, flatIndex: pageBase.value + i, label: p.title, sub: p.path })),
)

const totalCount = computed(() => pageBase.value + pages.value.length)

function selectByFlat(flat: number) {
  // 先找實體區塊
  let idx = 0
  for (const sec of SECTIONS) {
    const rows = data.value[sec.key] || []
    if (flat < idx + rows.length) { sec.navigate(rows[flat - idx]); close(); return }
    idx += rows.length
  }
  const page = pages.value[flat - idx]
  if (page) { router.push(page.path); close() }
}

let timer: ReturnType<typeof setTimeout> | null = null
watch(query, (q) => {
  activeIndex.value = -1
  if (timer) clearTimeout(timer)
  const s = q.trim()
  if (s.length < 2) { data.value = {}; isLoading.value = false; return }
  timer = setTimeout(async () => {
    isLoading.value = true
    try {
      const res = await globalSearch(s)
      data.value = (res.data as Record<string, Item[]>) || {}
    } catch {
      data.value = {}
    } finally {
      isLoading.value = false
    }
  }, 300)
})

function onKeydown(e: KeyboardEvent) {
  const total = totalCount.value
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    activeIndex.value = total ? (activeIndex.value + 1) % total : -1
    scrollActiveIntoView()
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    activeIndex.value = total ? (activeIndex.value - 1 + total) % total : -1
    scrollActiveIntoView()
  } else if (e.key === 'Enter') {
    e.preventDefault()
    if (activeIndex.value >= 0 && activeIndex.value < total) selectByFlat(activeIndex.value)
  } else if (e.key === 'Escape') {
    close()
  }
}

function scrollActiveIntoView() {
  nextTick(() => {
    resultsRef.value?.querySelector('.gs-item--active')?.scrollIntoView({ block: 'nearest' })
  })
}

function open() {
  visible.value = true
  query.value = ''
  activeIndex.value = -1
  data.value = {}
  nextTick(() => inputRef.value?.focus())
}

function close() {
  visible.value = false
  if (timer) clearTimeout(timer)
}

function onGlobalKeydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
    e.preventDefault()
    visible.value ? close() : open()
  }
}

onMounted(() => window.addEventListener('keydown', onGlobalKeydown))
onUnmounted(() => {
  window.removeEventListener('keydown', onGlobalKeydown)
  if (timer) clearTimeout(timer)
})

defineExpose({ open })
</script>
```

- [ ] **Step 4: 跑測試確認通過**

Run: `npx vitest run src/components/__tests__/GlobalSearch.spec.ts`
Expected: PASS（2 passed）

- [ ] **Step 5: typecheck**

Run: `npm run typecheck`
Expected: 0 error

- [ ] **Step 6: Commit**

```bash
git add src/components/GlobalSearch.vue src/components/__tests__/GlobalSearch.spec.ts
git commit -m "feat(search): 重寫後台全域搜尋面板（8 類 + 頁面導航 + 跳轉）"
```

---

## Phase D — 列表頁讀 query 預篩

> 只對「有名稱/關鍵字 filter」的列表頁接線。**班級（`ClassroomView`）與公告（`AnnouncementView`）目前無名稱搜尋 filter** → 搜尋面板對這兩類只 `router.push` 到該頁、不帶預篩（已在 Phase C 的 `navigate` 反映），本 Phase 不處理它們（避免為了預篩硬加 UI；列為 follow-up）。

### Task D1: 才藝報名 — 確認既有 `initFromQuery` 吃 `search`

**Files:**
- 確認：`src/views/activity/ActivityRegistrationView.vue` + 其 composable `useActivityRegistration`（`initFromQuery()`）

- [ ] **Step 1: 確認 query key 對齊**

讀 `useActivityRegistration` 的 `initFromQuery()`，確認它讀的 query key 與 Phase C `navigate` 帶的 `search` 一致。若它讀的是別的 key（例如 `keyword`），把 Phase C 該 section 的 `navigate` query key 改成一致（單一字串改動）。

- [ ] **Step 2: 手動驗證（dev server）**

開 `/activity/registrations?search=趙小妹`，確認列表自動以「趙小妹」篩選。

- [ ] **Step 3: Commit（若有改 Phase C 的 query key）**

```bash
git add src/components/GlobalSearch.vue
git commit -m "fix(search): 才藝報名導航 query key 對齊 initFromQuery"
```

> 若無需改動（key 已是 `search`），跳過 commit。

---

### Task D2: 員工列表 — 讀 `route.query.search` 預篩

**Files:**
- Modify: `src/views/EmployeeView.vue`（搜尋變數 `searchQuery: ref('')` @ line ~345；`debouncedSearch` @ line ~346）
- Test: `src/views/__tests__/EmployeeView.query.spec.ts`（新）

- [ ] **Step 1: 寫失敗測試**

```ts
// src/views/__tests__/EmployeeView.query.spec.ts
import { describe, it, expect, vi } from 'vitest'
// 以既有 EmployeeView 測試的 mount 樣板為準（mock store/api）。
// 核心斷言：route.query.search='陳' 時，元件 searchQuery 初始值 === '陳'。
// 若 EmployeeView 過重不易 mount，改為抽出純函式測試（見 Step 3 的 applySearchFromQuery）。
it.todo('route.query.search 帶入 searchQuery 初始值')
```

> 若 `EmployeeView` 因相依過多難以單測，採「抽純函式 + 單測純函式」策略（Step 3）。`it.todo` 僅佔位，Step 3 後改為真斷言或刪除。

- [ ] **Step 2: 實作 — 在 setup 讀 route.query**

在 `EmployeeView.vue` `<script setup>` 既有 `useRoute` 之後（若無則 `import { useRoute } from 'vue-router'` + `const route = useRoute()`），於 `searchQuery` 宣告後加：
```ts
// 全域搜尋導航帶入：?search=<關鍵字> → 預填搜尋框
if (typeof route.query.search === 'string' && route.query.search) {
  searchQuery.value = route.query.search
  debouncedSearch.value = route.query.search
}
```
（`debouncedSearch` 是觸發 `runEmployeeSearch` 的 watch 來源，兩者一起設可立即套用篩選。）

- [ ] **Step 3: 跑測試 / typecheck**

Run: `cd ~/Desktop/ivy-frontend && npm run typecheck`
Expected: 0 error
（若 Step 1 寫了真測試，`npx vitest run src/views/__tests__/EmployeeView.query.spec.ts` 應 PASS。）

- [ ] **Step 4: 手動驗證**

開 `/employees?section=employees&search=陳`，確認搜尋框預填「陳」且列表已篩選。

- [ ] **Step 5: Commit**

```bash
git add src/views/EmployeeView.vue src/views/__tests__/EmployeeView.query.spec.ts
git commit -m "feat(search): 員工列表讀 route.query.search 預篩"
```

---

### Task D3: 招生列表 — 讀 `route.query.keyword` 預篩

**Files:**
- Modify: `src/views/RecruitmentView.vue`（`filter: ref(...)` 含 `keyword` @ line ~624）

- [ ] **Step 1: 實作 — setup 讀 query**

在 `RecruitmentView.vue` `<script setup>` 內，於 `filter` 宣告後加（若無 `useRoute` 先補 import）：
```ts
const route = useRoute()
if (typeof route.query.keyword === 'string' && route.query.keyword) {
  filter.value.keyword = route.query.keyword
}
```
確認元件初次載入會用 `filter.keyword` 觸發查詢（若初次查詢在 onMounted 才跑，這樣設好即可；若需手動觸發，呼叫其載入函式）。

- [ ] **Step 2: typecheck**

Run: `npm run typecheck`
Expected: 0 error

- [ ] **Step 3: 手動驗證**

開 `/recruitment?keyword=趙小寶`，確認招生列表以「趙小寶」篩選。

- [ ] **Step 4: Commit**

```bash
git add src/views/RecruitmentView.vue
git commit -m "feat(search): 招生列表讀 route.query.keyword 預篩"
```

---

### Task D4: 學費列表 — 讀 `route.query.search` 預篩（threaded 到 FeeRecordsTab）

**Files:**
- Modify: `src/views/StudentFeeView.vue`（mount 時呼叫 `feeRecordsTabRef.value?.fetchRecords?.()` @ line ~76）
- Modify: `src/components/fees/FeeRecordsTab.vue`（`recordFilter: ref` 含 `student_name` @ line ~224；`v-model="recordFilter.student_name"` @ line ~32）

- [ ] **Step 1: FeeRecordsTab 開放 expose 一個 setter**

在 `FeeRecordsTab.vue` `<script setup>` 加並 `defineExpose`（若已有 defineExpose 則併入）：
```ts
function applySearch(name: string) {
  recordFilter.value.student_name = name
  fetchRecords()
}
defineExpose({ fetchRecords, applySearch })
```

- [ ] **Step 2: StudentFeeView mount 時讀 query 套用**

在 `StudentFeeView.vue` `<script setup>`（補 `import { useRoute } from 'vue-router'` + `const route = useRoute()`），把既有 mount 呼叫改為：
```ts
onMounted(async () => {
  await nextTick()
  const kw = typeof route.query.search === 'string' ? route.query.search : ''
  if (kw) feeRecordsTabRef.value?.applySearch?.(kw)
  else feeRecordsTabRef.value?.fetchRecords?.()
})
```
（保留原本無 query 時的 `fetchRecords()` 行為。）

- [ ] **Step 3: typecheck**

Run: `npm run typecheck`
Expected: 0 error

- [ ] **Step 4: 手動驗證**

開 `/fees?search=趙小妹`，確認學費紀錄分頁以「趙小妹」篩選。

- [ ] **Step 5: Commit**

```bash
git add src/views/StudentFeeView.vue src/components/fees/FeeRecordsTab.vue
git commit -m "feat(search): 學費紀錄讀 route.query.search 預篩"
```

---

## Phase E — 整合驗證

### Task E1: 雙端整合 + Ctrl+K 手動走一輪

- [ ] **Step 1: 起兩端**

Run: `cd ~/Desktop/ivyManageSystem && ./start.sh`
（後端 :8088 / 前端 :5173）

- [ ] **Step 2: 用 admin 帳號登入後台，按 Ctrl+K，逐類驗證**

- 輸入一個學生姓名片段 → 出現「學生」「家長」「學費」「才藝報名」等區塊 → 點學生跳 `/students/profile/:id`、點學費跳 `/fees?search=` 且已預篩。
- 輸入員工姓名 → 點員工跳 `/employees?...&search=` 且預篩。
- 輸入班級名 → 出現「班級」「頁面」→ 點班級跳 `/classrooms`。
- 輸入「招生」幼生姓名 → 點招生跳 `/recruitment?keyword=` 且預篩。
- 輸入頁面關鍵字（例「薪資」）→「頁面」區塊出現且 `canAccessRoute` 過濾正確（無權頁面不出現）。
- ↑↓ 鍵盤導航 + Enter 跳轉 + Esc 關閉。

- [ ] **Step 3: 權限把關抽驗**

用一個非 admin 帳號（例如只有 `STUDENTS_READ` 的角色）登入，Ctrl+K 搜尋 → 確認只出現學生/家長等有權區塊，無權類別不出現。

- [ ] **Step 4: 收尾 gate**

Run: `cd ~/Desktop/ivyManageSystem && ./scripts/finish-check.sh`
（push 前確認；CI 綠需自行到 GitHub Actions 確認。）

---

## 落地檢查清單（push 前）

- [ ] 後端 `python -m pytest tests/test_search.py -v` 全綠
- [ ] `dump_openapi.py` + 前端 `gen:api:check` 乾淨
- [ ] 前端 `npm run typecheck` 0 error、`npx vitest run` 搜尋相關測試全綠、`npm run lint`（no-explicit-any gate）通過
- [ ] 整合手測一輪（Task E1）
- [ ] 前後端分開 commit、各自分支 off origin/main
- [ ] 後端先 push（無 migration、無 schema 變更）、前端後 push（含 schema.d.ts）
- [ ] **Definition of Done**：push + CI 綠 + worktree remove
