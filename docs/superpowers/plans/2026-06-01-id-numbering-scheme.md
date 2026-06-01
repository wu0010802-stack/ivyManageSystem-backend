# 學號與員工編號邏輯 實作計畫（Backend）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 學生學號改為「永久編號（不變）+ denormalized 顯示快取（反映當前學年/年級）」；員工工號改為自動產生 `{民國到職年}{流水}`（例 `114001`）。

**Architecture:** 學生身分認定鍵 = `(enrollment_school_year, enrollment_seq)`（永久、入學配發一次）。對外 `student_id` 仍是儲存欄位但語意改為「顯示快取」，由 `before_flush` event listener 在新建/換班時依當前班級重算為 `{學年}-{年級字}-{流水}`。`enrollment_seq` 為 nullable，listener 只處理「seq 非 NULL」的列 → 既有測試 fixture 完全不受影響。員工工號於 server 端用 advisory-lock 配發器產生。

**Tech Stack:** FastAPI、SQLAlchemy ORM、Alembic、PostgreSQL（prod）/ SQLite（測試 in-memory）、pytest。

**對應 spec:** `docs/superpowers/specs/2026-06-01-id-numbering-scheme-design.md`

**範圍界定（重要）：**
- 本計畫只涵蓋 **ivy-backend**。前端「新增員工表單移除工號輸入」屬 ivy-frontend、另一 repo、依 workspace SOP 後端先行 + 分開 commit，列為計畫尾端 follow-up，不在本計畫的 TDD 任務內。
- Part A（學生）與 Part B（員工）相互獨立，可分別 commit。Part A 先行（含 migration）。

---

## 檔案結構

**新增：**
- `services/student_numbering.py` — `grade_char` / `compute_student_display_id` / `next_enrollment_seq`
- `models/student_events.py` — `before_flush` listener，重算 `student_id` 快取
- `services/employee_numbering.py` — `next_employee_id`
- `alembic/versions/<rev>_student_enrollment_numbering.py` — 欄位 + 約束 + backfill
- 測試：`tests/test_student_numbering.py`、`tests/test_student_id_recompute_listener.py`、`tests/test_student_enrollment_backfill.py`、`tests/test_employee_numbering.py`

**修改：**
- `models/classroom.py` — Student 加兩欄、移除 `student_id` 的 `unique`、加複合唯一約束
- `models/__init__.py` — import `student_events` 註冊 listener
- `services/recruitment_conversion.py` — 配發 `enrollment_seq`、移除舊產號 + 唯一性檢查
- `api/students.py` — `StudentCreate` 移除 `student_id`、`create_student` 配發 seq、移除唯一性檢查
- `api/recruitment/records.py` — deprecated convert 端點 `student_id_code` 改 Optional
- `api/employees.py` — `EmployeeCreate`/`EmployeeUpdate` 移除 `employee_id`、`create_employee` 自動配號

---

## 並發與相容性備註（所有 create/conversion 任務適用）

- **複合唯一鍵是並發的真正後盾**：advisory lock 已用雙參數整數形式（跨 worker 穩定），
  但仍須把 DB 約束當最後防線。`create_student`、`convert_recruitment_to_student`、
  `create_employee` 三處的 `session.commit()` 須 catch `sqlalchemy.exc.IntegrityError`，
  回乾淨的 409/400「請重試」而非裸 500（極罕見的並發撞號落到約束時）。實作時於各 commit
  外層加 `except IntegrityError: session.rollback(); raise HTTPException(409, "編號配發衝突，請重試")`
  （conversion 內層改 raise `RecruitmentConversionError`）。
- **後端可先於前端部署**：`StudentCreate` / `EmployeeCreate` 皆繼承純 `BaseModel`
  （Pydantic 預設 `extra="ignore"`）。移除欄位後，舊前端仍 POST `student_id`/`employee_id`
  會被**靜默忽略**（自動配號勝出），不會 422 → 後端先 merge + `alembic upgrade heads` 安全。
  實作時確認這兩個 schema 沒有改用 `extra="forbid"` 的嚴格基底。

---

# Part A — 學生學號（backend）

### Task A1: Student model 加永久編號欄位 + 調整約束

**Files:**
- Modify: `models/classroom.py`（Student class，130-228；`__table_args__` 在 220-224）
- Test: `tests/test_student_numbering.py`

- [ ] **Step 1: 寫失敗測試（model 欄位 + 複合唯一 + student_id 可重複）**

建立 `tests/test_student_numbering.py`：

```python
import os
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Student


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


class TestStudentEnrollmentColumns:
    def test_columns_exist_and_nullable(self, session):
        # 不設 enrollment_seq 也能建（nullable）→ 既有 fixture 相容
        stu = Student(student_id="LEGACY-1", name="舊生")
        session.add(stu)
        session.flush()
        assert stu.enrollment_school_year is None
        assert stu.enrollment_seq is None

    def test_student_id_no_longer_unique(self, session):
        # student_id 變顯示快取，允許罕見重複（跨屆留級生同顯示碼）
        session.add(Student(student_id="115-中-05", name="A"))
        session.add(Student(student_id="115-中-05", name="B"))
        session.flush()  # 不應因 unique 而炸

    def test_enrollment_key_composite_unique(self, session):
        session.add(
            Student(student_id="x1", name="A", enrollment_school_year=114, enrollment_seq=1)
        )
        session.flush()
        session.add(
            Student(student_id="x2", name="B", enrollment_school_year=114, enrollment_seq=1)
        )
        with pytest.raises(Exception):
            session.flush()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_student_numbering.py -v`
Expected: FAIL（`enrollment_school_year` 屬性不存在 / student_id 仍 unique）

- [ ] **Step 3: 改 Student model**

在 `models/classroom.py` 的 Student class 內：

把 `student_id` 那行的 `unique=True` 拿掉（保留 index 由約束處理）：

```python
    student_id = Column(String(20), nullable=False, index=True, comment="學號（顯示快取；身分認定見 enrollment_school_year+seq）")
```

在 `classroom_id` 欄位附近新增兩欄（放在 `recruitment_visit_id` 之後即可）：

```python
    enrollment_school_year = Column(
        Integer, nullable=True, comment="發號學年（民國）；身分認定鍵之一，永久不變"
    )
    enrollment_seq = Column(
        Integer, nullable=True, comment="永久流水號；入學配發一次、終身不變"
    )
```

把 `__table_args__` 改成（新增複合唯一約束）：

```python
    __table_args__ = (
        Index("ix_student_classroom", "classroom_id", "is_active"),
        Index("ix_student_enrollment_grad", "enrollment_date", "graduation_date"),
        Index("ix_student_lifecycle_status", "lifecycle_status"),
        UniqueConstraint(
            "enrollment_school_year",
            "enrollment_seq",
            name="uq_students_enrollment_year_seq",
        ),
    )
```

（`UniqueConstraint` 與 `Index` 已在檔頭 import；確認無誤。）

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_student_numbering.py -v`
Expected: PASS（3 個）

- [ ] **Step 5: Commit**

```bash
git add models/classroom.py tests/test_student_numbering.py
git commit -m "feat(student): 加 enrollment_school_year/seq 永久編號欄位 + 複合唯一鍵，student_id 改顯示快取（去 unique）"
```

---

### Task A2: 學號顯示純函式 + seq 配發器

**Files:**
- Create: `services/student_numbering.py`
- Test: `tests/test_student_numbering.py`（追加）

- [ ] **Step 1: 追加失敗測試**

在 `tests/test_student_numbering.py` 末端追加（沿用上面的 `session` fixture）：

```python
from models.classroom import Classroom, ClassGrade
from services.student_numbering import (
    grade_char,
    compute_student_display_id,
    next_enrollment_seq,
)


def _grade(session, name, sort_order=1):
    g = ClassGrade(name=name, sort_order=sort_order)
    session.add(g)
    session.flush()
    return g


def _classroom(session, *, school_year, grade=None, name="班", code="A"):
    c = Classroom(
        name=name, school_year=school_year, semester=1,
        grade_id=(grade.id if grade else None), class_code=code,
    )
    session.add(c)
    session.flush()
    return c


class TestGradeChar:
    def test_first_char(self):
        assert grade_char("大班") == "大"
        assert grade_char("幼幼班") == "幼"
    def test_blank(self):
        assert grade_char(None) == ""
        assert grade_char("  ") == ""


class TestComputeDisplayId:
    def test_in_classroom_with_grade(self, session):
        g = _grade(session, "中班")
        c = _classroom(session, school_year=115, grade=g)
        stu = Student(student_id="tmp", name="A", classroom_id=c.id,
                      enrollment_school_year=114, enrollment_seq=5)
        session.add(stu); session.flush()
        assert compute_student_display_id(session, stu) == "115-中-05"

    def test_classroom_without_grade(self, session):
        c = _classroom(session, school_year=115, grade=None)
        stu = Student(student_id="tmp", name="A", classroom_id=c.id,
                      enrollment_school_year=114, enrollment_seq=5)
        session.add(stu); session.flush()
        assert compute_student_display_id(session, stu) == "115-05"

    def test_no_classroom_fallback(self, session):
        stu = Student(student_id="tmp", name="A", classroom_id=None,
                      enrollment_school_year=114, enrollment_seq=5)
        session.add(stu); session.flush()
        assert compute_student_display_id(session, stu) == "114-05"

    def test_seq_none_returns_existing(self, session):
        stu = Student(student_id="LEGACY", name="A", enrollment_seq=None)
        session.add(stu); session.flush()
        assert compute_student_display_id(session, stu) == "LEGACY"


class TestNextEnrollmentSeq:
    def test_first_is_one(self, session):
        assert next_enrollment_seq(session, 114) == 1

    def test_increments_within_year(self, session):
        session.add(Student(student_id="a", name="A",
                            enrollment_school_year=114, enrollment_seq=1))
        session.add(Student(student_id="b", name="B",
                            enrollment_school_year=114, enrollment_seq=2))
        session.flush()
        assert next_enrollment_seq(session, 114) == 3

    def test_per_year_independent(self, session):
        session.add(Student(student_id="a", name="A",
                            enrollment_school_year=114, enrollment_seq=7))
        session.flush()
        assert next_enrollment_seq(session, 115) == 1
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_student_numbering.py -v`
Expected: FAIL（`services.student_numbering` 不存在）

- [ ] **Step 3: 建立 `services/student_numbering.py`**

```python
"""services/student_numbering.py — 學生永久編號配發 + 學號顯示快取組字。

身分認定鍵 = (Student.enrollment_school_year, Student.enrollment_seq)，永久不變。
對外 student_id 是「由當前班級 + 永久 seq 組出的顯示快取」，由 before_flush
listener（models/student_events.py）維護。本模組只提供純函式 + 配發器。
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session


def grade_char(grade_name: Optional[str]) -> str:
    """年級名稱首字：大班→大、中班→中、小班→小、幼幼班→幼；空白回 ''。"""
    return (grade_name or "").strip()[:1]


def compute_student_display_id(session: Session, student) -> Optional[str]:
    """組出學號顯示快取。

    - 有班級且有年級： {classroom.school_year}-{年級字}-{seq:02d}
    - 有班級無年級：   {classroom.school_year}-{seq:02d}
    - 無班級：         {enrollment_school_year}-{seq:02d}
    - enrollment_seq 為 None（legacy/未配號）：原樣回傳 student.student_id（不接管）
    """
    from models.classroom import Classroom, ClassGrade

    seq = student.enrollment_seq
    if seq is None:
        return student.student_id

    classroom = (
        session.get(Classroom, student.classroom_id)
        if student.classroom_id
        else None
    )
    if classroom is not None:
        gname = None
        if classroom.grade_id:
            grade = session.get(ClassGrade, classroom.grade_id)
            gname = grade.name if grade else None
        gc = grade_char(gname)
        if gc:
            return f"{classroom.school_year}-{gc}-{seq:02d}"
        return f"{classroom.school_year}-{seq:02d}"

    year = student.enrollment_school_year
    return f"{year}-{seq:02d}" if year is not None else f"{seq:02d}"


def next_enrollment_seq(session: Session, school_year: int) -> int:
    """配發指定發號學年的下一個永久 seq（該學年內 max+1）。

    Postgres 上以 pg_advisory_xact_lock 防並發撞號（lock 涵蓋整個 transaction）。
    SQLite/其他 dialect 跳過 lock。
    """
    from models.classroom import Student

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        # 雙參數 advisory lock：固定整數 namespace + 整數 year。
        # 不可用 hash(str)（PYTHONHASHSEED 每 process 隨機 → 跨 worker 無互斥）。
        session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :year)"),
            {"ns": _LOCK_NS_ENROLLMENT, "year": int(school_year)},
        )

    max_seq = (
        session.query(func.max(Student.enrollment_seq))
        .filter(Student.enrollment_school_year == school_year)
        .scalar()
    )
    return (max_seq or 0) + 1
```

> 在 `services/student_numbering.py` 檔頭常數區加：`_LOCK_NS_ENROLLMENT = 1001`
> （固定整數 namespace，跨 process 穩定）。

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_student_numbering.py -v`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add services/student_numbering.py tests/test_student_numbering.py
git commit -m "feat(student): 加學號顯示組字純函式 grade_char/compute_student_display_id + seq 配發器"
```

---

### Task A3: before_flush listener 自動重算 student_id

**Files:**
- Create: `models/student_events.py`
- Modify: `models/__init__.py`（註冊 listener）
- Test: `tests/test_student_id_recompute_listener.py`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_student_id_recompute_listener.py`：

```python
import os
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
import models  # noqa: F401  確保 listener 已註冊
from models.classroom import Student, Classroom, ClassGrade


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _grade(session, name):
    g = ClassGrade(name=name, sort_order=1)
    session.add(g); session.flush()
    return g


def _classroom(session, school_year, grade):
    c = Classroom(name="班", school_year=school_year, semester=1,
                  grade_id=grade.id, class_code="A")
    session.add(c); session.flush()
    return c


class TestRecomputeListener:
    def test_computed_on_insert(self, session):
        c = _classroom(session, 114, _grade(session, "小班"))
        stu = Student(student_id="will-be-overwritten", name="A",
                      classroom_id=c.id, enrollment_school_year=114, enrollment_seq=5)
        session.add(stu); session.flush()
        assert stu.student_id == "114-小-05"

    def test_recomputed_on_classroom_change(self, session):
        small = _classroom(session, 114, _grade(session, "小班"))
        mid = _classroom(session, 115, _grade(session, "中班"))
        stu = Student(student_id="x", name="A", classroom_id=small.id,
                      enrollment_school_year=114, enrollment_seq=5)
        session.add(stu); session.flush()
        assert stu.student_id == "114-小-05"
        stu.classroom_id = mid.id  # 升年級
        session.flush()
        assert stu.student_id == "115-中-05"  # seq 不變

    def test_seq_none_not_touched(self, session):
        c = _classroom(session, 114, _grade(session, "小班"))
        stu = Student(student_id="LEGACY-1", name="A", classroom_id=c.id)  # 無 seq
        session.add(stu); session.flush()
        assert stu.student_id == "LEGACY-1"  # listener 略過
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_student_id_recompute_listener.py -v`
Expected: FAIL（insert 後 student_id 仍是 "will-be-overwritten"）

- [ ] **Step 3: 建立 listener 並註冊**

建立 `models/student_events.py`：

```python
"""models/student_events.py — Student.student_id 顯示快取的 before_flush 維護。

對 session 內「新建」或「classroom_id/enrollment_seq 有異動」且 enrollment_seq
非 NULL 的 Student，重算 student_id 顯示快取。涵蓋所有 ORM 寫入路徑
（報到分班 insert、bulk_transfer 與 PUT /students 的屬性 set）。

不涵蓋 query.update(synchronize_session=False) 的 bulk path（如 classroom_carry_over
同學年 1→2），但該 path 同學年同年級、顯示值不變，故無需重算。
"""

from sqlalchemy import event
from sqlalchemy.orm import Session


@event.listens_for(Session, "before_flush")
def _recompute_student_display_id(session, flush_context, instances):
    from models.classroom import Student
    from services.student_numbering import compute_student_display_id

    targets = [
        obj
        for obj in (set(session.new) | set(session.dirty))
        if isinstance(obj, Student)
    ]
    if not targets:
        return

    with session.no_autoflush:
        for stu in targets:
            if getattr(stu, "enrollment_seq", None) is None:
                continue
            new_id = compute_student_display_id(session, stu)
            if new_id and stu.student_id != new_id:
                stu.student_id = new_id
```

在 `models/__init__.py` 結尾加入（確保 model 載入時註冊 listener；放在 Student 所屬的 `classroom` import 之後）：

```python
from . import student_events  # noqa: F401  註冊 before_flush listener
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_student_id_recompute_listener.py -v`
Expected: PASS（3 個）

- [ ] **Step 5: Commit**

```bash
git add models/student_events.py models/__init__.py tests/test_student_id_recompute_listener.py
git commit -m "feat(student): before_flush listener 依當前班級自動重算 student_id 顯示快取"
```

---

### Task A4: 招生轉化改走 seq 配發器

**Files:**
- Modify: `services/recruitment_conversion.py`（41-127）
- Test: `tests/test_student_numbering.py`（追加整合測試）

- [ ] **Step 1: 追加失敗測試**

先確認既有測試對此函式的依賴：

Run: `grep -rn "student_id_code" tests --include="*.py"`
若有測試斷言「轉化後 student.student_id == 傳入的 code」，於本任務 Step 4 後一併更新為斷言新格式（見下）。

在 `tests/test_student_numbering.py` 追加（用 conversion 真實流程；沿用 `session` fixture）：

```python
def test_conversion_allocates_enrollment_seq(session, monkeypatch):
    from models.recruitment import RecruitmentVisit
    from services import recruitment_conversion as rc
    # 固定當前學年為 114（避免依賴系統時鐘）
    monkeypatch.setattr(rc, "resolve_current_academic_term", lambda *a, **k: (114, 1))

    g = _grade(session, "小班")
    c = _classroom(session, school_year=114, grade=g)
    visit = RecruitmentVisit(child_name="小明")
    session.add(visit); session.flush()

    result = rc.convert_recruitment_to_student(
        session, recruitment_visit_id=visit.id, classroom_id=c.id,
    )
    stu = session.get(Student, result.student_id)
    assert stu.enrollment_school_year == 114
    assert stu.enrollment_seq == 1
    assert stu.student_id == "114-小-01"  # listener 已組出顯示快取
```

> 註：`RecruitmentVisit` 必填欄位若不只 `child_name`，依該 model 補齊最小欄位（見 `models/recruitment.py`）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_student_numbering.py::test_conversion_allocates_enrollment_seq -v`
Expected: FAIL（enrollment_seq 為 None / student_id 非新格式）

- [ ] **Step 3: 改寫 `convert_recruitment_to_student`**

在 `services/recruitment_conversion.py`：

把產號 + 唯一性區塊（現 81-108，從 `# 學號自動產生` 到 `raise RecruitmentConversionError(f"學號已存在：{code}")`）整段替換為：

```python
    # 永久編號配發（取代舊 next_student_id_code + 全域唯一性檢查）。
    # student_id_code 參數已 deprecated：學號顯示由 enrollment_seq + 當前班級
    # 經 before_flush listener 自動組出，不再接受手填。
    from services.student_numbering import next_enrollment_seq

    enroll_year, _ = resolve_current_academic_term()
    seq = next_enrollment_seq(session, enroll_year)
```

把 `Student(...)` 建構（現 112-125）改為：移除 `student_id=code,`，加入兩個永久欄位：

```python
    student = Student(
        name=(visit.child_name or "").strip() or "未命名",
        gender=gender,
        birthday=visit.birthday,
        classroom_id=classroom_id,
        enrollment_school_year=enroll_year,
        enrollment_seq=seq,
        enrollment_date=enroll_date,
        lifecycle_status=initial_lifecycle_status,
        recruitment_visit_id=visit.id,
        parent_phone=visit.phone,  # 快照欄位
        address=visit.address,
        notes=(visit.notes or None),
        is_active=True,
    )
    session.add(student)
    session.flush()  # 取得 student.id；listener 已於 flush 前組出 student_id
```

> `student_id_code` 函式參數保留於簽章（caller 相容），docstring 標注 deprecated/ignored。
> 舊變數 `code` 已不再使用，確認移除後無殘留引用。

- [ ] **Step 4: 跑測試確認通過 + 更新受影響既有測試**

Run: `pytest tests/test_student_numbering.py -v`
Expected: PASS

Run: `pytest tests/ -k "recruit or conversion" -v`
Expected: 既有「斷言 student_id == 手填 code」的測試會 FAIL → 更新為斷言新格式
（`{學年}-{年級字}-{seq:02d}`）或改斷言 `enrollment_seq`。逐一修正至 PASS。

- [ ] **Step 5: Commit**

```bash
git add services/recruitment_conversion.py tests/
git commit -m "feat(student): 招生轉化改配發 enrollment_seq、移除手填學號與全域唯一性檢查"
```

---

### Task A5: POST /students 改自動配號

**Files:**
- Modify: `api/students.py`（`StudentCreate` 168+；`create_student` 806-851）
- Test: `tests/test_student_numbering.py`（追加，或既有 students API 測試）

- [ ] **Step 1: 追加失敗測試**

在 `tests/test_student_numbering.py` 追加（純服務層驗證 create 路徑會配 seq；用既有 session）：

```python
def test_post_students_path_sets_enrollment_seq(session, monkeypatch):
    """模擬 create_student 核心：建學生時配 seq、不手填 student_id。"""
    from services.student_numbering import next_enrollment_seq
    g = _grade(session, "中班")
    c = _classroom(session, school_year=115, grade=g)

    seq = next_enrollment_seq(session, 115)
    stu = Student(name="新生", classroom_id=c.id,
                  enrollment_school_year=115, enrollment_seq=seq)
    session.add(stu); session.flush()
    assert stu.enrollment_seq == 1
    assert stu.student_id == "115-中-01"
```

- [ ] **Step 2: 跑測試確認失敗（若 model 尚未支援則 FAIL；A1 後應已 PASS 此純 model 行為）**

Run: `pytest tests/test_student_numbering.py::test_post_students_path_sets_enrollment_seq -v`
Expected: PASS（此步驗證 model+listener；真正 API 改動在 Step 3，無獨立單元測試時以此守住核心行為）

- [ ] **Step 3: 改 `StudentCreate` 與 `create_student`**

在 `api/students.py` 的 `StudentCreate`（168）移除這行：

```python
    student_id: str = Field(..., min_length=1, max_length=20)
```

把 `create_student`（806-844）的本體改為（移除唯一性檢查、配發 seq）：

```python
    session = get_session()
    try:
        data = item.model_dump()

        # 永久編號配發（取代手填 student_id + 唯一性檢查）。
        # student_id 顯示快取由 before_flush listener 依當前班級自動組出。
        from services.student_numbering import next_enrollment_seq

        enroll_year, _ = resolve_current_academic_term()
        seq = next_enrollment_seq(session, enroll_year)
        data["enrollment_school_year"] = enroll_year
        data["enrollment_seq"] = seq

        student = Student(**data)
        session.add(student)
        session.flush()  # 取得 student.id；listener 已組出 student_id

        # 自動寫入「入學」異動紀錄
        from models.student_log import StudentChangeLog

        school_year, semester = resolve_current_academic_term()
        enrollment_date = student.enrollment_date or today_taipei()
        change_log = StudentChangeLog(
            student_id=student.id,
            school_year=school_year,
            semester=semester,
            event_type="入學",
            event_date=enrollment_date,
            classroom_id=student.classroom_id,
            reason="新生報名",
            recorded_by=current_user.get("user_id"),
        )
        session.add(change_log)
        session.commit()
        return {"message": "學生新增成功", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="新增失敗")
    finally:
        session.close()
```

> 確認 `resolve_current_academic_term` 與 `today_taipei` 已在 `api/students.py` import（檔內既有使用）。

- [ ] **Step 4: 跑測試確認通過 + 更新既有 students API 測試**

Run: `pytest tests/test_student_numbering.py -v`
Expected: PASS

Run: `pytest tests/ -k "create_student or students_api or test_students" -v`
Expected: 既有「POST /students 帶 student_id 並斷言該值」的測試需更新（移除 student_id 入參、改斷言新格式）。逐一修至 PASS。

- [ ] **Step 5: Commit**

```bash
git add api/students.py tests/
git commit -m "feat(student): POST /students 改自動配發 enrollment_seq，StudentCreate 移除手填學號"
```

---

### Task A6: deprecated 招生 convert 端點 student_id_code 改 Optional

**Files:**
- Modify: `api/recruitment/records.py`（`ConvertRecordRequest` 308-316）

- [ ] **Step 1: 改 schema（無新測試；行為由 A4 覆蓋，此處只解除必填）**

把 `ConvertRecordRequest.student_id_code`（309）改為 Optional：

```python
class ConvertRecordRequest(BaseModel):
    student_id_code: Optional[str] = Field(
        None, max_length=20, description="[deprecated] 已忽略；學號改自動產生"
    )
    classroom_id: Optional[int] = None
    enrollment_date: Optional[date] = None
    gender: Optional[str] = Field(None, max_length=10)
    initial_lifecycle_status: str = Field(
        LIFECYCLE_ENROLLED,
        description="預設 enrolled（已報到未開學）；若直接入學可設 active",
    )
```

> 此端點本已 `deprecated=True`；`convert_recruitment_to_student` 已忽略 `student_id_code`。
> 確認 `Optional` 已 import（檔內既有使用）。

- [ ] **Step 2: 跑相關測試**

Run: `pytest tests/ -k "convert" -v`
Expected: PASS（不再要求必填 student_id_code）

- [ ] **Step 3: Commit**

```bash
git add api/recruitment/records.py
git commit -m "feat(recruitment): deprecated convert 端點 student_id_code 改 Optional（學號改自動產生）"
```

---

### Task A7: Alembic migration — 欄位 + 約束 + backfill

先把 backfill 邏輯抽成可測純函式，再寫 migration 呼叫它。

**Files:**
- Modify: `services/student_numbering.py`（加 `backfill_enrollment_numbers`）
- Create: `alembic/versions/<rev>_student_enrollment_numbering.py`
- Test: `tests/test_student_enrollment_backfill.py`

- [ ] **Step 1: 寫 backfill 失敗測試**

建立 `tests/test_student_enrollment_backfill.py`：

```python
import os
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
import models  # noqa: F401
from models.classroom import Student, Classroom, ClassGrade
from services.student_numbering import backfill_enrollment_numbers


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def test_backfill_parses_year_and_reseqs_per_year(session):
    # 兩位 114 學年學生，舊格式跨班同 NN（01）會碰撞 → backfill 須在學年內重排
    session.add(Student(id=1, student_id="114-A-01", name="甲"))
    session.add(Student(id=2, student_id="114-B-01", name="乙"))
    session.add(Student(id=3, student_id="115-A-03", name="丙"))
    session.flush()

    backfill_enrollment_numbers(session)
    session.flush()

    s1 = session.get(Student, 1)
    s2 = session.get(Student, 2)
    s3 = session.get(Student, 3)
    assert s1.enrollment_school_year == 114 and s2.enrollment_school_year == 114
    assert {s1.enrollment_seq, s2.enrollment_seq} == {1, 2}  # 學年內唯一重排
    assert s3.enrollment_school_year == 115 and s3.enrollment_seq == 1


def test_backfill_unparseable_uses_enrollment_date_then_idempotent(session):
    from datetime import date
    session.add(Student(id=1, student_id="LEGACYX", name="無前綴",
                        enrollment_date=date(2025, 9, 1)))  # 2025-09 → 114 學年
    session.flush()
    backfill_enrollment_numbers(session)
    session.flush()
    s1 = session.get(Student, 1)
    assert s1.enrollment_school_year == 114
    assert s1.enrollment_seq == 1
    # 冪等：再跑不應重配（已有 seq 的列跳過）
    backfill_enrollment_numbers(session)
    session.flush()
    assert session.get(Student, 1).enrollment_seq == 1
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_student_enrollment_backfill.py -v`
Expected: FAIL（`backfill_enrollment_numbers` 不存在）

- [ ] **Step 3: 在 `services/student_numbering.py` 追加 backfill**

```python
import re
from datetime import date

_OLD_STUDENT_ID_PREFIX_RE = re.compile(r"^(\d{3})-")


def _roc_year_from_date(d: Optional[date]) -> Optional[int]:
    """日期 → 學年（民國）：8 月起算當學年，否則前一學年。"""
    if d is None:
        return None
    base = d.year if d.month >= 8 else d.year - 1
    return base - 1911


def backfill_enrollment_numbers(session: Session) -> int:
    """為既有學生回填 enrollment_school_year + enrollment_seq（冪等）。

    - 已有 enrollment_seq 的列跳過（冪等）。
    - 發號學年：解析 student_id 前綴 ^(\\d{3})- → 失敗用 enrollment_date 推算 →
      再失敗用當前學年。
    - seq：在每個發號學年內，依 id 排序對「尚未配號」者接續 max(現有 seq)+1。

    回傳處理筆數。
    """
    from models.classroom import Student
    from utils.academic import resolve_current_academic_term

    cur_year, _ = resolve_current_academic_term()

    pending = (
        session.query(Student)
        .filter(Student.enrollment_seq.is_(None))
        .order_by(Student.id)
        .all()
    )
    if not pending:
        return 0

    # 每個學年目前最大 seq（含已配號者）作為續編起點
    year_max: dict[int, int] = {}
    for (yr, mx) in (
        session.query(Student.enrollment_school_year, func.max(Student.enrollment_seq))
        .filter(Student.enrollment_seq.isnot(None))
        .group_by(Student.enrollment_school_year)
        .all()
    ):
        if yr is not None:
            year_max[yr] = mx or 0

    count = 0
    for stu in pending:
        year = None
        m = _OLD_STUDENT_ID_PREFIX_RE.match(stu.student_id or "")
        if m:
            year = int(m.group(1))
        if year is None:
            year = _roc_year_from_date(stu.enrollment_date)
        if year is None:
            year = cur_year

        nxt = year_max.get(year, 0) + 1
        year_max[year] = nxt
        stu.enrollment_school_year = year
        stu.enrollment_seq = nxt
        # listener 不會對 bulk-loaded 既有列觸發 → 此處直接重算 student_id 顯示快取
        stu.student_id = compute_student_display_id(session, stu) or stu.student_id
        count += 1

    return count
```

> `compute_student_display_id` 與 `backfill_enrollment_numbers` 同檔，直接呼叫即可（免 import）。

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_student_enrollment_backfill.py -v`
Expected: PASS（2 個）

- [ ] **Step 5: 建立 Alembic migration**

先找當前 head：

Run: `cd /Users/yilunwu/Desktop/ivy-backend && alembic heads`
記下 head revision id 作為 `down_revision`。

建立 `alembic/versions/<rev>_student_enrollment_numbering.py`（`revision` 自取如 `studnum01`，`down_revision` = 上面 head）：

```python
"""student enrollment numbering: 永久編號欄位 + 約束 + backfill

Revision ID: studnum01
Revises: <CURRENT_HEAD>
Create Date: 2026-06-01

downgrade 不可逆部分：student_id 還原為新格式快取（{學年}-{年級字}-{seq}），
非原始 {學年}-{班代}-{NN}（class_code 已不在 student_id 內）。
"""
from alembic import op
import sqlalchemy as sa

revision = "studnum01"
down_revision = "<CURRENT_HEAD>"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "students",
        sa.Column("enrollment_school_year", sa.Integer(), nullable=True),
    )
    op.add_column(
        "students",
        sa.Column("enrollment_seq", sa.Integer(), nullable=True),
    )
    # 移除 student_id 的 unique（Postgres unique=True 預設約束名 students_student_id_key）
    op.drop_constraint("students_student_id_key", "students", type_="unique")
    op.create_index("ix_students_student_id", "students", ["student_id"])
    op.create_unique_constraint(
        "uq_students_enrollment_year_seq",
        "students",
        ["enrollment_school_year", "enrollment_seq"],
    )

    # backfill（用 ORM session 跑可測純函式）
    from sqlalchemy.orm import Session
    from services.student_numbering import backfill_enrollment_numbers

    bind = op.get_bind()
    sess = Session(bind=bind)
    backfill_enrollment_numbers(sess)
    sess.commit()


def downgrade():
    op.drop_constraint(
        "uq_students_enrollment_year_seq", "students", type_="unique"
    )
    op.drop_index("ix_students_student_id", table_name="students")
    op.create_unique_constraint(
        "students_student_id_key", "students", ["student_id"]
    )
    op.drop_column("students", "enrollment_seq")
    op.drop_column("students", "enrollment_school_year")
```

> ⚠️ Step 5 注意：
> - 用 `alembic heads` 確認**單一 head**；若多 head 需先補 merge revision。
> - `students_student_id_key` 為 Postgres 對 `unique=True` 的預設約束名。實作時用
>   `\d students`（psql）或 `SELECT conname FROM pg_constraint WHERE conrelid='students'::regclass`
>   核對真實名稱再填入。
> - 交給 `migration-reviewer` agent 審查可逆性與破壞性 DDL。

- [ ] **Step 6: Commit**

```bash
git add services/student_numbering.py alembic/versions/ tests/test_student_enrollment_backfill.py
git commit -m "feat(student): backfill 函式 + alembic migration（永久編號欄位/約束/回填）"
```

---

# Part B — 員工工號（backend）

### Task B1: 工號配發器

**Files:**
- Create: `services/employee_numbering.py`
- Test: `tests/test_employee_numbering.py`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_employee_numbering.py`：

```python
import os
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
import models  # noqa: F401
from models.employee import Employee
from services.employee_numbering import next_employee_id


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


class TestNextEmployeeId:
    def test_first_of_year(self, session):
        assert next_employee_id(session, 114) == "114001"

    def test_increments_within_year(self, session):
        session.add(Employee(employee_id="114001", name="A"))
        session.add(Employee(employee_id="114002", name="B"))
        session.flush()
        assert next_employee_id(session, 114) == "114003"

    def test_per_year_independent(self, session):
        session.add(Employee(employee_id="114007", name="A"))
        session.flush()
        assert next_employee_id(session, 115) == "115001"

    def test_ignores_legacy_nonmatching_format(self, session):
        # 舊手填工號 E001 / ADMIN001 不影響新流水
        session.add(Employee(employee_id="E001", name="A"))
        session.add(Employee(employee_id="ADMIN001", name="B"))
        session.flush()
        assert next_employee_id(session, 114) == "114001"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_employee_numbering.py -v`
Expected: FAIL（模組不存在）

- [ ] **Step 3: 建立 `services/employee_numbering.py`**

```python
"""services/employee_numbering.py — 員工工號自動配發。

格式：{民國到職年:03d}{當年流水:03d}，例 114001。到職即固定、與班級/職務無關。
舊手填工號（E001/ADMIN001 等）不符此格式者一律忽略，不影響流水。
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

_EMP_ID_RE = re.compile(r"^(\d{3})(\d{3,})$")
_LOCK_NS_EMPLOYEE = 1002  # 固定整數 advisory lock namespace（跨 process 穩定）


def next_employee_id(session: Session, hire_year_roc: int) -> str:
    """配發指定到職民國年的下一個工號。

    Postgres 以 pg_advisory_xact_lock 防並發撞號；SQLite 跳過。
    """
    from models.employee import Employee

    prefix = f"{hire_year_roc:03d}"
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        # 雙參數 advisory lock：固定整數 namespace + 整數 year。
        # 不可用 hash(str)（PYTHONHASHSEED 每 process 隨機 → 跨 worker 無互斥）。
        session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :year)"),
            {"ns": _LOCK_NS_EMPLOYEE, "year": int(hire_year_roc)},
        )

    rows = (
        session.query(Employee.employee_id)
        .filter(Employee.employee_id.like(f"{prefix}%"))
        .all()
    )
    max_seq = 0
    for (eid,) in rows:
        m = _EMP_ID_RE.match(eid or "")
        if m and m.group(1) == prefix:
            max_seq = max(max_seq, int(m.group(2)))
    return f"{prefix}{max_seq + 1:03d}"
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_employee_numbering.py -v`
Expected: PASS（4 個）

- [ ] **Step 5: Commit**

```bash
git add services/employee_numbering.py tests/test_employee_numbering.py
git commit -m "feat(employee): 工號配發器 next_employee_id（{民國到職年}{流水}）"
```

---

### Task B2: create_employee 自動配號 + schema 調整

**Files:**
- Modify: `api/employees.py`（`EmployeeCreate` 147+；`EmployeeUpdate`；`create_employee` 400+ 重複檢查區塊與回傳）
- Test: `tests/test_employee_numbering.py`（追加核心行為）

- [ ] **Step 1: 追加失敗測試（核心：建員工時工號自動產生）**

在 `tests/test_employee_numbering.py` 追加：

```python
def test_create_path_assigns_id_from_hire_date(session):
    """模擬 create_employee 核心：依 hire_date 民國年配工號。"""
    from datetime import date
    hire = date(2025, 9, 1)  # 民國 114
    hire_year_roc = hire.year - 1911
    eid = next_employee_id(session, hire_year_roc)
    session.add(Employee(employee_id=eid, name="新人", hire_date=hire))
    session.flush()
    assert eid == "114001"
```

- [ ] **Step 2: 跑測試確認失敗/通過**

Run: `pytest tests/test_employee_numbering.py::test_create_path_assigns_id_from_hire_date -v`
Expected: PASS（驗證配發器+model；API 改動在 Step 3）

- [ ] **Step 3: 改 schema 與 create_employee**

在 `api/employees.py`：

`EmployeeCreate`（147）移除這行：

```python
    employee_id: str
```

`EmployeeUpdate` 移除其 `employee_id: Optional[str] = None`（工號不可改）。

`create_employee`（400+）把重複檢查區塊（現「# 檢查工號是否重複」到 `BusinessError(...)` 整段）刪除，改為在 `emp_data` 與日期解析之後、建立 `Employee(**emp_data)` 之前插入自動配號：

```python
        emp_data = emp.model_dump()
        # 處理日期欄位（保留既有迴圈）
        for _field in _DATE_FIELDS:
            parsed = parse_optional_date(emp_data.get(_field))
            if parsed:
                emp_data[_field] = parsed
            else:
                emp_data.pop(_field, None)

        # 自動配發工號（取代手填 + 重複檢查）：取 hire_date 民國年，空則用今日
        from datetime import date as _date
        from services.employee_numbering import next_employee_id
        from utils.taipei_time import today_taipei

        _hire = emp_data.get("hire_date")
        _hire_year = (_hire.year if isinstance(_hire, _date) else today_taipei().year)
        emp_data["employee_id"] = next_employee_id(session, _hire_year - 1911)
```

並確認 `create_employee` 的回傳 dict 含產生的工號，讓前端顯示（在 commit 後的 return 加上 `"employee_id": employee.employee_id`；若現回傳變數名非 `employee`，依實際變數名調整）。

> `today_taipei` 若已在檔頭 import 則免重複；以上用區域 import 確保不漏。

- [ ] **Step 4: 跑測試確認通過 + 更新既有員工 API 測試**

Run: `pytest tests/test_employee_numbering.py -v`
Expected: PASS

Run: `pytest tests/ -k "employee and (create or api)" -v`
Expected: 既有「POST /employees 帶 employee_id 並斷言該值」的測試需更新（移除入參、改斷言自動格式或僅斷言建立成功）。逐一修至 PASS。

- [ ] **Step 5: Commit**

```bash
git add api/employees.py tests/test_employee_numbering.py
git commit -m "feat(employee): create_employee 自動配號、EmployeeCreate/Update 移除手填工號"
```

---

# 全套驗證

- [ ] **Step 1: 跑相關測試群**

Run: `pytest tests/test_student_numbering.py tests/test_student_id_recompute_listener.py tests/test_student_enrollment_backfill.py tests/test_employee_numbering.py -v`
Expected: 全 PASS

- [ ] **Step 2: 確認相對 main 無新增 fail**

Run: `pytest tests/ -q 2>&1 | tail -30`
Expected: 失敗數不高於 main baseline（既有 pre-existing fail 不算回歸）。逐一比對新增 fail 是否本計畫造成；造成者修正。

- [ ] **Step 3: migration 健檢**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && alembic heads`（確認單一 head）
請 `migration-reviewer` agent 審查 `studnum01` 的可逆性、破壞性 DDL、約束名正確性。

---

# Follow-up（不在本計畫 TDD 範圍）

1. **前端（ivy-frontend，另 repo、另 commit）**：新增員工表單移除/唯讀「工號」輸入，
   建立後顯示後端回傳的 `employee_id`。依 workspace SOP，後端 merge + `alembic upgrade heads`
   後再做。先定位表單檔（grep `employee_id` 於員工管理 view / `constants/employeeFields.ts`）。
2. **OpenAPI codegen**：後端 schema 改動後，依 CLAUDE.md 跑 `dump_openapi.py` + 前端 `gen:api`，
   commit `schema.d.ts`。
3. **既有列印學號變更告知**：migration 會改既有 `student_id` 字串；上線前通知家長/行政。
4. **prod migration**：merge 後 `alembic upgrade heads`（PostgreSQL）。
5. **清理**：`services/recruitment_funnel.py::next_student_id_code` 與其測試在 A4 後若無其他
   引用（`grep -rn next_student_id_code`）可移除；`repositories/student.py:17` dead
   `get_by_student_id` 一併評估移除。
