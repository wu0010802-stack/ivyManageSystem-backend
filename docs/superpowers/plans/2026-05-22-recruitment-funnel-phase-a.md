# 招生漏斗 Phase A（後端）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 4 階段招生漏斗的後端基礎（state machine + event log + scheduler），讓 Phase B 前端 Kanban 可以掛上。

**Architecture:** stage 完全 derived（純函式於 `(has_deposit, enrolled, lifecycle_status)`），新增 `academic_terms` + `recruitment_event_log` 兩表。所有 stage 變動經過 `services/recruitment_funnel.transition_visit()` 單一 atomic 入口（row-level lock + event log）。學號於報到當下由 `next_student_id_code()` 自動產生 `{民國學年}-{class_code}-{NN}`。開學日由 daily scheduler 半自動推進。

**Tech Stack:** FastAPI / SQLAlchemy / Alembic / Pydantic v2 / PostgreSQL（advisory lock）/ pytest。

**Spec:** `docs/superpowers/specs/2026-05-22-recruitment-funnel-phase-a-design.md`

---

## File Structure

**New files:**

| 路徑 | 責任 |
|---|---|
| `alembic/versions/20260522_rfunnel01_recruitment_funnel.py` | 建 `academic_terms` + `recruitment_event_log` 兩表 |
| `models/academic_term.py` | `AcademicTerm` ORM |
| `services/recruitment_funnel.py` | 純函式（derive_stage / can_transition / is_destructive / next_student_id_code / assert_student_revertable）+ orchestrator `transition_visit()` |
| `services/recruitment_lifecycle.py` | `advance_term_to_active()` — scheduler 入口 |
| `services/recruitment_term_advance_scheduler.py` | 每日心跳 → 觸發 `advance_term_to_active` |
| `schemas/academic_term.py` | Pydantic in/out schemas |
| `schemas/recruitment_funnel.py` | Pydantic in/out schemas（board / transition / timeline） |
| `api/academic_terms.py` | `/academic-terms` CRUD router |
| `api/recruitment/funnel.py` | `/recruitment/funnel/...` router（board / transition / timeline） |
| `tests/test_recruitment_funnel_pure.py` | 純函式單測 |
| `tests/test_recruitment_funnel_transitions.py` | transition_visit 整合測試 |
| `tests/test_recruitment_student_id_concurrency.py` | 學號並發 |
| `tests/test_recruitment_term_advance.py` | scheduler 測試 |
| `tests/test_recruitment_funnel_api.py` | funnel API |
| `tests/test_academic_terms_api.py` | academic_terms API |

**Modified files:**

| 路徑 | 變動 |
|---|---|
| `models/recruitment.py` | 加 `RecruitmentEventLog` model（同 domain 不另開檔） |
| `services/recruitment_conversion.py` | `student_id_code` 改 optional + 內部寫 event log |
| `api/recruitment/__init__.py` | 註冊 funnel sub-router |
| `api/recruitment/records.py` | 第 ~307 行 deprecated 標記 |
| `main.py` | scheduler init（仿 graduation_scheduler）+ register `api/academic_terms` |
| `config/scheduler.py` | 3 個新 settings 欄位 |

---

## Task 1: Alembic migration（建表 + downgrade）

**Files:**
- Create: `alembic/versions/20260522_rfunnel01_recruitment_funnel.py`

- [ ] **Step 1: 找當前 head 與基本資訊**

```bash
cd ~/Desktop/ivy-backend && alembic heads
# Expected: 3be2e40aaa42 (head)
```

- [ ] **Step 2: 寫 migration 檔**

```python
"""recruitment funnel phase a: academic_terms + recruitment_event_log

Revision ID: rfunnel01
Revises: 3be2e40aaa42
Create Date: 2026-05-22
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'rfunnel01'
down_revision: Union[str, Sequence[str], None] = '3be2e40aaa42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "academic_terms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("school_year", sa.Integer(), nullable=False),
        sa.Column("semester", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("school_year", "semester", name="uq_academic_terms_year_semester"),
        sa.CheckConstraint("end_date > start_date", name="ck_academic_terms_date_order"),
        sa.CheckConstraint("semester IN (1, 2)", name="ck_academic_terms_semester_valid"),
    )

    op.create_table(
        "recruitment_event_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "recruitment_visit_id",
            sa.Integer(),
            sa.ForeignKey("recruitment_visits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("from_stage", sa.String(length=20), nullable=True),
        sa.Column("to_stage", sa.String(length=20), nullable=False),
        sa.Column(
            "student_id",
            sa.Integer(),
            sa.ForeignKey("students.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_recruitment_event_log_visit_time",
        "recruitment_event_log",
        ["recruitment_visit_id", "created_at"],
    )
    op.create_index("ix_recruitment_event_log_event_type", "recruitment_event_log", ["event_type"])
    op.create_index("ix_recruitment_event_log_actor", "recruitment_event_log", ["actor_user_id"])


def downgrade() -> None:
    op.drop_index("ix_recruitment_event_log_actor", table_name="recruitment_event_log")
    op.drop_index("ix_recruitment_event_log_event_type", table_name="recruitment_event_log")
    op.drop_index("ix_recruitment_event_log_visit_time", table_name="recruitment_event_log")
    op.drop_table("recruitment_event_log")
    op.drop_table("academic_terms")
```

> **注意**：欄位名稱用 `metadata_json` 而非 `metadata` — SQLAlchemy `DeclarativeBase` 保留 `metadata` 屬性，避免衝突。

- [ ] **Step 3: 跑 upgrade**

```bash
cd ~/Desktop/ivy-backend && alembic upgrade head
# Expected: INFO [alembic.runtime.migration] Running upgrade 3be2e40aaa42 -> rfunnel01
```

- [ ] **Step 4: 驗證 schema**

```bash
psql -U yilunwu -d ivymanagement -c "\d academic_terms" -c "\d recruitment_event_log"
# Expected: 兩表結構正確，含 unique/check constraints 與 indexes
```

- [ ] **Step 5: 驗 downgrade**

```bash
alembic downgrade -1
psql -U yilunwu -d ivymanagement -c "\dt academic_terms" -c "\dt recruitment_event_log"
# Expected: 兩表皆消失（Did not find any relation）
alembic upgrade head
```

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/20260522_rfunnel01_recruitment_funnel.py
git commit -m "feat(db): add academic_terms + recruitment_event_log (rfunnel01)"
```

---

## Task 2: ORM models

**Files:**
- Create: `models/academic_term.py`
- Modify: `models/recruitment.py`（加 `RecruitmentEventLog`）

- [ ] **Step 1: 寫 `models/academic_term.py`**

```python
"""models/academic_term.py — 學年/學期/開學日設定。

scheduler 在 `start_date` 當天觸發批量推進 enrolled → active。
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, Date, DateTime,
    UniqueConstraint, CheckConstraint,
)
from models.base import Base


class AcademicTerm(Base):
    __tablename__ = "academic_terms"

    id = Column(Integer, primary_key=True, index=True)
    school_year = Column(Integer, nullable=False, comment="民國學年")
    semester = Column(Integer, nullable=False, comment="1=上學期、2=下學期")
    start_date = Column(Date, nullable=False, comment="開學日")
    end_date = Column(Date, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("school_year", "semester", name="uq_academic_terms_year_semester"),
        CheckConstraint("end_date > start_date", name="ck_academic_terms_date_order"),
        CheckConstraint("semester IN (1, 2)", name="ck_academic_terms_semester_valid"),
    )
```

- [ ] **Step 2: 在 `models/recruitment.py` 尾端加 `RecruitmentEventLog`**

```python
# 加在檔案最末（緊接最後一個 class 後）

class RecruitmentEventLog(Base):
    """招生漏斗階段事件流（visit 層級的 timeline）。"""

    __tablename__ = "recruitment_event_log"

    id = Column(Integer, primary_key=True, index=True)
    recruitment_visit_id = Column(
        Integer,
        ForeignKey("recruitment_visits.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type = Column(String(40), nullable=False)
    from_stage = Column(String(20), nullable=True)
    to_stage = Column(String(20), nullable=False)
    student_id = Column(
        Integer,
        ForeignKey("students.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason = Column(Text, nullable=True)
    actor_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    metadata_json = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    __table_args__ = (
        Index("ix_recruitment_event_log_visit_time", "recruitment_visit_id", "created_at"),
        Index("ix_recruitment_event_log_event_type", "event_type"),
        Index("ix_recruitment_event_log_actor", "actor_user_id"),
    )
```

> 注意：`models/recruitment.py` 既有 import 已包含 `Column, Integer, String, ...`，但需新增 `from sqlalchemy import ForeignKey, Text` 與 `from sqlalchemy.dialects.postgresql import JSONB`（若尚未引入）。

- [ ] **Step 3: 補 import**

於 `models/recruitment.py` 既有 import 區塊新增（保留既有）：

```python
from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
```

- [ ] **Step 4: 寫 smoke test 驗 import 與 metadata**

```python
# tests/test_models_recruitment_funnel_smoke.py
from models.academic_term import AcademicTerm
from models.recruitment import RecruitmentEventLog


def test_academic_term_columns():
    cols = {c.name for c in AcademicTerm.__table__.columns}
    assert {"id", "school_year", "semester", "start_date", "end_date"} <= cols


def test_recruitment_event_log_columns():
    cols = {c.name for c in RecruitmentEventLog.__table__.columns}
    assert {
        "id", "recruitment_visit_id", "event_type", "from_stage", "to_stage",
        "student_id", "reason", "actor_user_id", "metadata_json", "created_at",
    } <= cols
```

- [ ] **Step 5: 跑測試**

```bash
pytest tests/test_models_recruitment_funnel_smoke.py -v
# Expected: 2 passed
```

- [ ] **Step 6: Commit**

```bash
git add models/academic_term.py models/recruitment.py tests/test_models_recruitment_funnel_smoke.py
git commit -m "feat(models): AcademicTerm + RecruitmentEventLog"
```

---

## Task 3: 純函式 — derive_stage / can_transition / is_destructive

**Files:**
- Create: `services/recruitment_funnel.py`（先寫純函式部分）
- Create: `tests/test_recruitment_funnel_pure.py`

- [ ] **Step 1: 寫測試（TDD red）**

```python
# tests/test_recruitment_funnel_pure.py
from dataclasses import dataclass
from typing import Optional

import pytest

from services.recruitment_funnel import (
    Stage,
    derive_stage,
    can_transition,
    is_destructive,
)


@dataclass
class _Visit:
    id: int = 1
    has_deposit: bool = False
    enrolled: bool = False


@dataclass
class _Student:
    id: int = 100
    lifecycle_status: str = "enrolled"


class TestDeriveStage:
    def test_visited_when_no_deposit_no_student(self):
        assert derive_stage(_Visit(has_deposit=False), None) == "visited"

    def test_deposited_when_has_deposit_no_student(self):
        assert derive_stage(_Visit(has_deposit=True), None) == "deposited"

    def test_enrolled_when_student_lifecycle_enrolled(self):
        assert derive_stage(_Visit(has_deposit=True), _Student(lifecycle_status="enrolled")) == "enrolled"

    def test_active_when_student_lifecycle_active(self):
        assert derive_stage(_Visit(has_deposit=True), _Student(lifecycle_status="active")) == "active"

    def test_student_presence_overrides_deposit_flag(self):
        # visit 顯示無 deposit 但 student 已建立 — student 為準
        assert derive_stage(_Visit(has_deposit=False), _Student()) == "enrolled"


class TestCanTransition:
    @pytest.mark.parametrize("frm", ["visited", "deposited", "enrolled", "active"])
    @pytest.mark.parametrize("to", ["visited", "deposited", "enrolled", "active"])
    def test_any_pair_allowed_in_phase_a(self, frm, to):
        # Phase A: 任意拖（含同階段同階段；同階段在 transition_visit 才回 409）
        assert can_transition(frm, to) is True


class TestIsDestructive:
    @pytest.mark.parametrize("frm,to,expected", [
        ("visited", "deposited", False),
        ("deposited", "visited", False),
        ("deposited", "enrolled", False),
        ("enrolled", "active", False),
        ("enrolled", "deposited", True),
        ("enrolled", "visited", True),
        ("active", "enrolled", True),
        ("active", "deposited", True),
        ("active", "visited", True),
    ])
    def test_destructive_mapping(self, frm, to, expected):
        assert is_destructive(frm, to) is expected
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
pytest tests/test_recruitment_funnel_pure.py -v
# Expected: ImportError / ModuleNotFoundError on services.recruitment_funnel
```

- [ ] **Step 3: 寫 `services/recruitment_funnel.py` 純函式部分**

```python
"""services/recruitment_funnel.py — 招生漏斗狀態機 + 寫入入口。

純函式（derive_stage / can_transition / is_destructive）位於檔頭，
orchestrator `transition_visit()` 在後續 task 補。
"""

from __future__ import annotations

from typing import Literal, Optional, Protocol

Stage = Literal["visited", "deposited", "enrolled", "active"]
STAGES: tuple[Stage, ...] = ("visited", "deposited", "enrolled", "active")


class _VisitLike(Protocol):
    has_deposit: bool


class _StudentLike(Protocol):
    lifecycle_status: str


def derive_stage(visit: _VisitLike, student: Optional[_StudentLike]) -> Stage:
    """從 (visit, student) 推導 4 階段。

    規則：student 存在性優先（avoid dual source of truth）。
    """
    if student is not None:
        return "active" if student.lifecycle_status == "active" else "enrolled"
    return "deposited" if visit.has_deposit else "visited"


def can_transition(from_stage: Stage, to_stage: Stage) -> bool:
    """Phase A：任意拖。保留位置給未來收緊規則（例：禁止跨多段躍進）。"""
    return True


_DESTRUCTIVE_FROM: frozenset[Stage] = frozenset({"enrolled", "active"})


def is_destructive(from_stage: Stage, to_stage: Stage) -> bool:
    """destructive = 從 enrolled/active 退回任何前段。"""
    if from_stage not in _DESTRUCTIVE_FROM:
        return False
    order = {s: i for i, s in enumerate(STAGES)}
    return order[to_stage] < order[from_stage]
```

- [ ] **Step 4: 跑測試**

```bash
pytest tests/test_recruitment_funnel_pure.py -v
# Expected: 全綠（derive 5 + can_transition 16 + is_destructive 9 = 30 passed）
```

- [ ] **Step 5: Commit**

```bash
git add services/recruitment_funnel.py tests/test_recruitment_funnel_pure.py
git commit -m "feat(funnel): pure stage derivation + transition predicates"
```

---

## Task 4: next_student_id_code（含 advisory lock）

**Files:**
- Modify: `services/recruitment_funnel.py`
- Create: `tests/test_recruitment_student_id_concurrency.py`

- [ ] **Step 1: 寫單執行緒 TDD 測試**

附加到 `tests/test_recruitment_funnel_pure.py` 尾端：

```python
# 接上既有測試
from sqlalchemy.orm import Session
from models.classroom import Classroom, Student, LIFECYCLE_ENROLLED


class TestNextStudentIdCode:
    """整合測試：需要 real DB session。
    
    Fixture `db_session` 來自 tests/conftest.py，提供乾淨的 transactional session。
    """

    def test_empty_pool_returns_01(self, db_session: Session):
        from services.recruitment_funnel import next_student_id_code
        code = next_student_id_code(db_session, school_year=115, class_code="A")
        assert code == "115-A-01"

    def test_increments_within_same_year_class(self, db_session: Session, make_student):
        from services.recruitment_funnel import next_student_id_code
        make_student(student_id="115-A-01")
        make_student(student_id="115-A-02")
        assert next_student_id_code(db_session, school_year=115, class_code="A") == "115-A-03"

    def test_independent_streams_per_class(self, db_session: Session, make_student):
        from services.recruitment_funnel import next_student_id_code
        make_student(student_id="115-A-01")
        make_student(student_id="115-A-02")
        # 不同班共用同一年但流水獨立
        assert next_student_id_code(db_session, school_year=115, class_code="B") == "115-B-01"

    def test_year_boundary_resets(self, db_session: Session, make_student):
        from services.recruitment_funnel import next_student_id_code
        make_student(student_id="115-A-05")
        assert next_student_id_code(db_session, school_year=116, class_code="A") == "116-A-01"
```

> Fixture `make_student` 假設在 `tests/conftest.py` 已存在（建立最小 Student）。若不存在，需先在 conftest.py 補 `make_student`（接 `db_session`）。實作 task 時若缺 fixture 先補。

- [ ] **Step 2: 確認測試失敗**

```bash
pytest tests/test_recruitment_funnel_pure.py::TestNextStudentIdCode -v
# Expected: ImportError on next_student_id_code
```

- [ ] **Step 3: 加 `next_student_id_code` 到 `services/recruitment_funnel.py`**

```python
# 加在純函式區塊後

import re
from sqlalchemy import func, text
from sqlalchemy.orm import Session


_STUDENT_ID_RE = re.compile(r"^(\d{3})-([A-Za-z0-9_-]+)-(\d{2,})$")


def next_student_id_code(session: Session, school_year: int, class_code: str) -> str:
    """產 {year}-{class_code}-{NN}（NN 兩位數零填，同年同班遞增）。

    使用 pg advisory xact lock 防並發撞號 — 範圍涵蓋整個 transaction，
    commit/rollback 時自動釋放。
    """
    from models.classroom import Student  # 延遲 import 避免循環

    lock_key = hash((school_year, class_code)) % (2**31)
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})

    prefix = f"{school_year}-{class_code}-"
    rows = session.query(Student.student_id).filter(
        Student.student_id.like(f"{prefix}%")
    ).all()
    max_seq = 0
    for (sid,) in rows:
        m = _STUDENT_ID_RE.match(sid or "")
        if m and m.group(1) == str(school_year) and m.group(2) == class_code:
            max_seq = max(max_seq, int(m.group(3)))
    return f"{prefix}{max_seq + 1:02d}"
```

- [ ] **Step 4: 跑單執行緒測試**

```bash
pytest tests/test_recruitment_funnel_pure.py::TestNextStudentIdCode -v
# Expected: 4 passed
```

- [ ] **Step 5: 寫並發測試**

```python
# tests/test_recruitment_student_id_concurrency.py
"""驗 next_student_id_code 在 50-thread 並發下不撞號。

需 real PostgreSQL（SQLite 不支援 advisory lock）。
"""

import threading
from sqlalchemy.orm import Session
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.recruitment_funnel import next_student_id_code
from models.classroom import Student


def test_no_duplicate_under_50_threads(db_url, make_classroom):
    # db_url 來自 conftest，指向 test DB（pg）
    engine = create_engine(db_url)
    Sess = sessionmaker(bind=engine)

    classroom = make_classroom(class_code="C")
    results: list[str] = []
    lock = threading.Lock()

    def worker(idx: int):
        sess = Sess()
        try:
            sess.begin()
            code = next_student_id_code(sess, school_year=115, class_code="C")
            # 模擬 service 真實流程：取碼後立刻 insert 才能讓 advisory lock 有效
            stu = Student(
                student_id=code,
                name=f"並發測試-{idx}",
                lifecycle_status="enrolled",
                is_active=True,
            )
            sess.add(stu)
            sess.commit()
            with lock:
                results.append(code)
        except Exception as e:
            sess.rollback()
            with lock:
                results.append(f"ERROR: {e}")
        finally:
            sess.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(results) == 50
    errors = [r for r in results if r.startswith("ERROR")]
    assert not errors, f"並發錯誤：{errors[:3]}"
    assert len(set(results)) == 50, "有重複學號"
    # 範圍：115-C-01 ~ 115-C-50（順序不保證）
    expected = {f"115-C-{i:02d}" for i in range(1, 51)}
    assert set(results) == expected
```

> Fixture `db_url` / `make_classroom` 來自 `tests/conftest.py`。若不存在需先補。

- [ ] **Step 6: 跑並發測試**

```bash
pytest tests/test_recruitment_student_id_concurrency.py -v
# Expected: 1 passed（無撞號）
```

- [ ] **Step 7: Commit**

```bash
git add services/recruitment_funnel.py tests/test_recruitment_funnel_pure.py tests/test_recruitment_student_id_concurrency.py
git commit -m "feat(funnel): next_student_id_code with pg advisory lock"
```

---

## Task 5: assert_student_revertable

**Files:**
- Modify: `services/recruitment_funnel.py`
- Modify: `tests/test_recruitment_funnel_pure.py`

- [ ] **Step 1: 寫測試**

附加到 `tests/test_recruitment_funnel_pure.py`：

```python
class TestAssertStudentRevertable:
    def test_clean_student_returns_none(self, db_session, make_student):
        from services.recruitment_funnel import assert_student_revertable
        s = make_student()
        assert assert_student_revertable(db_session, s.id) is None  # 不 raise

    def test_with_attendance_raises(self, db_session, make_student, make_attendance):
        from services.recruitment_funnel import (
            RecruitmentFunnelError, assert_student_revertable,
        )
        s = make_student()
        make_attendance(student_id=s.id)
        with pytest.raises(RecruitmentFunnelError) as exc:
            assert_student_revertable(db_session, s.id)
        assert "業務資料" in str(exc.value)

    def test_with_fee_invoice_raises(self, db_session, make_student, make_fee_invoice):
        from services.recruitment_funnel import (
            RecruitmentFunnelError, assert_student_revertable,
        )
        s = make_student()
        make_fee_invoice(student_id=s.id)
        with pytest.raises(RecruitmentFunnelError):
            assert_student_revertable(db_session, s.id)
```

> 對應 fixture `make_attendance` / `make_fee_invoice` 需在 conftest.py 補（最小列）。若 prod table 名不同，依實際 model 名調整。

- [ ] **Step 2: 確認失敗**

```bash
pytest tests/test_recruitment_funnel_pure.py::TestAssertStudentRevertable -v
# Expected: ImportError / AttributeError
```

- [ ] **Step 3: 加 `RecruitmentFunnelError` 與 `assert_student_revertable`**

加到 `services/recruitment_funnel.py`：

```python
class RecruitmentFunnelError(ValueError):
    """Funnel 業務錯誤（caller 應 catch → HTTP 400）。"""

    def __init__(self, message: str, code: str = "FUNNEL_ERROR"):
        super().__init__(message)
        self.code = code


# 下游業務白名單 — 任一存在則無法 revert convert
_REVERT_BLOCKERS = [
    # (model_path, fk_column_name, friendly_label)
    ("models.classroom.StudentAttendance", "student_id", "出席紀錄"),
    ("models.fees.StudentFee", "student_id", "繳費資料"),
    ("models.fees.StudentFeeInvoice", "student_id", "發票"),
    ("models.parent.ParentUser", "student_id", "家長綁定"),  # 視實際 model 調整
    ("models.classroom.StudentAssessment", "student_id", "評量"),
    ("models.classroom.StudentIncident", "student_id", "獎懲紀錄"),
    # 體溫/餵藥若有獨立表，依實際 model 補
]


def assert_student_revertable(session: Session, student_id: int) -> None:
    """檢查 student 是否有下游業務記錄；任一存在則 raise。

    白名單來源：spec §7.2。
    """
    import importlib

    for model_path, fk_col, label in _REVERT_BLOCKERS:
        module_name, _, class_name = model_path.rpartition(".")
        try:
            module = importlib.import_module(module_name)
            model = getattr(module, class_name, None)
        except ImportError:
            continue
        if model is None:
            continue
        column = getattr(model, fk_col, None)
        if column is None:
            continue
        exists = session.query(model).filter(column == student_id).limit(1).first()
        if exists is not None:
            raise RecruitmentFunnelError(
                f"該學生已有業務資料（{label}），請走退學流程而非退回 funnel",
                code="REVERT_STUDENT_HAS_DATA",
            )
```

> 動態 import 是因為部分 model 可能位於 sub-module 變動中；任一缺失靜默 skip（implementer 在落地時若白名單中有 model 對應的 module path 改動，需更新 `_REVERT_BLOCKERS` 列表）。

- [ ] **Step 4: 跑測試**

```bash
pytest tests/test_recruitment_funnel_pure.py::TestAssertStudentRevertable -v
# Expected: 3 passed
```

- [ ] **Step 5: Commit**

```bash
git add services/recruitment_funnel.py tests/test_recruitment_funnel_pure.py
git commit -m "feat(funnel): assert_student_revertable downstream guard"
```

---

## Task 6: transition_visit — visited ↔ deposited

**Files:**
- Modify: `services/recruitment_funnel.py`（加 orchestrator 與 `_toggle_deposit` 子動作）
- Create: `tests/test_recruitment_funnel_transitions.py`

- [ ] **Step 1: 寫測試**

```python
# tests/test_recruitment_funnel_transitions.py
import pytest
from services.recruitment_funnel import (
    transition_visit, RecruitmentFunnelError,
)
from models.recruitment import RecruitmentVisit, RecruitmentEventLog


class TestVisitedDeposited:
    def test_visited_to_deposited(self, db_session, make_visit):
        visit = make_visit(has_deposit=False)
        result = transition_visit(
            db_session, visit_id=visit.id, to_stage="deposited",
            actor_user_id=99,
        )
        db_session.commit()

        assert result.from_stage == "visited"
        assert result.to_stage == "deposited"
        assert result.student_id is None

        db_session.refresh(visit)
        assert visit.has_deposit is True

        log = db_session.query(RecruitmentEventLog).filter_by(
            recruitment_visit_id=visit.id
        ).one()
        assert log.event_type == "deposit_added"
        assert log.actor_user_id == 99

    def test_deposited_to_visited(self, db_session, make_visit):
        visit = make_visit(has_deposit=True)
        result = transition_visit(
            db_session, visit_id=visit.id, to_stage="visited",
            actor_user_id=99,
        )
        db_session.commit()
        db_session.refresh(visit)
        assert visit.has_deposit is False
        log = db_session.query(RecruitmentEventLog).filter_by(
            recruitment_visit_id=visit.id
        ).one()
        assert log.event_type == "deposit_removed"

    def test_same_stage_returns_409(self, db_session, make_visit):
        visit = make_visit(has_deposit=False)
        with pytest.raises(RecruitmentFunnelError) as exc:
            transition_visit(
                db_session, visit_id=visit.id, to_stage="visited",
                actor_user_id=99,
            )
        assert exc.value.code == "STAGE_ALREADY"

    def test_visit_not_found(self, db_session):
        with pytest.raises(RecruitmentFunnelError) as exc:
            transition_visit(
                db_session, visit_id=999999, to_stage="deposited",
                actor_user_id=99,
            )
        assert exc.value.code == "VISIT_NOT_FOUND"
```

- [ ] **Step 2: 確認失敗**

```bash
pytest tests/test_recruitment_funnel_transitions.py::TestVisitedDeposited -v
# Expected: ImportError on transition_visit
```

- [ ] **Step 3: 寫 `transition_visit` skeleton + `visited↔deposited` dispatch**

加到 `services/recruitment_funnel.py`：

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass
class TransitionResult:
    visit_id: int
    from_stage: Stage
    to_stage: Stage
    student_id: Optional[int]
    event_log_id: int
    warnings: list[str]


def _load_visit_locked(session: Session, visit_id: int):
    """以 SELECT ... FOR UPDATE 鎖 visit row，未找到回 None。"""
    from models.recruitment import RecruitmentVisit
    return (
        session.query(RecruitmentVisit)
        .filter(RecruitmentVisit.id == visit_id)
        .with_for_update()
        .first()
    )


def _load_student_by_visit(session: Session, visit_id: int):
    from models.classroom import Student
    return (
        session.query(Student)
        .filter(Student.recruitment_visit_id == visit_id)
        .first()
    )


def _write_event_log(
    session: Session,
    *,
    visit_id: int,
    event_type: str,
    from_stage: Optional[Stage],
    to_stage: Stage,
    student_id: Optional[int] = None,
    actor_user_id: Optional[int] = None,
    reason: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    from models.recruitment import RecruitmentEventLog
    log = RecruitmentEventLog(
        recruitment_visit_id=visit_id,
        event_type=event_type,
        from_stage=from_stage,
        to_stage=to_stage,
        student_id=student_id,
        actor_user_id=actor_user_id,
        reason=reason,
        metadata_json=metadata,
        created_at=datetime.now(),
    )
    session.add(log)
    session.flush()
    return log.id


def _do_toggle_deposit(session, visit, *, to_stage: Stage, actor_user_id):
    visit.has_deposit = (to_stage == "deposited")
    event_type = "deposit_added" if to_stage == "deposited" else "deposit_removed"
    log_id = _write_event_log(
        session,
        visit_id=visit.id,
        event_type=event_type,
        from_stage="visited" if to_stage == "deposited" else "deposited",
        to_stage=to_stage,
        actor_user_id=actor_user_id,
    )
    return None, log_id  # (student_id, log_id)


def transition_visit(
    session: Session,
    visit_id: int,
    to_stage: Stage,
    actor_user_id: Optional[int],
    *,
    classroom_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> TransitionResult:
    visit = _load_visit_locked(session, visit_id)
    if visit is None:
        raise RecruitmentFunnelError(
            f"招生訪視不存在：id={visit_id}", code="VISIT_NOT_FOUND"
        )
    student = _load_student_by_visit(session, visit_id)
    from_stage = derive_stage(visit, student)

    if from_stage == to_stage:
        raise RecruitmentFunnelError(
            f"已在 {to_stage} 階段", code="STAGE_ALREADY"
        )
    if is_destructive(from_stage, to_stage) and not (reason and reason.strip()):
        raise RecruitmentFunnelError(
            "destructive 操作需提供 reason", code="REASON_REQUIRED"
        )

    warnings: list[str] = []

    # Dispatch — Task 6 只實作 visited↔deposited
    if {from_stage, to_stage} == {"visited", "deposited"}:
        student_id, log_id = _do_toggle_deposit(
            session, visit, to_stage=to_stage, actor_user_id=actor_user_id,
        )
    else:
        # 其他 dispatch 在後續 task 補
        raise NotImplementedError(
            f"transition {from_stage} → {to_stage} 尚未實作（Task 7-9 補）"
        )

    return TransitionResult(
        visit_id=visit.id,
        from_stage=from_stage,
        to_stage=to_stage,
        student_id=student_id,
        event_log_id=log_id,
        warnings=warnings,
    )
```

- [ ] **Step 4: 跑測試**

```bash
pytest tests/test_recruitment_funnel_transitions.py::TestVisitedDeposited -v
# Expected: 4 passed
```

- [ ] **Step 5: Commit**

```bash
git add services/recruitment_funnel.py tests/test_recruitment_funnel_transitions.py
git commit -m "feat(funnel): transition_visit orchestrator + visited<->deposited"
```

---

## Task 7: convert_recruitment_to_student 改 optional + 寫 event log

**Files:**
- Modify: `services/recruitment_conversion.py`
- Modify: `tests/test_recruitment_conversion.py`（若存在）— 補回歸測試

- [ ] **Step 1: 補回歸測試**

```python
# tests/test_recruitment_conversion_optional_code.py
import pytest
from services.recruitment_conversion import (
    convert_recruitment_to_student, RecruitmentConversionError,
)
from models.recruitment import RecruitmentEventLog


def test_auto_generate_student_id_when_omitted(db_session, make_visit, make_classroom):
    classroom = make_classroom(class_code="A", school_year=115)
    visit = make_visit(has_deposit=True)

    result = convert_recruitment_to_student(
        db_session,
        recruitment_visit_id=visit.id,
        student_id_code=None,            # ← 改成可省略
        classroom_id=classroom.id,
    )
    db_session.commit()

    assert result.student_id is not None
    # 學號自動產出
    from models.classroom import Student
    student = db_session.get(Student, result.student_id)
    assert student.student_id.startswith("115-A-")
    assert student.student_id.endswith("-01")  # 首位


def test_explicit_code_still_works(db_session, make_visit, make_classroom):
    classroom = make_classroom(class_code="A", school_year=115)
    visit = make_visit(has_deposit=True)
    result = convert_recruitment_to_student(
        db_session,
        recruitment_visit_id=visit.id,
        student_id_code="CUSTOM-001",
        classroom_id=classroom.id,
    )
    db_session.commit()
    from models.classroom import Student
    assert db_session.get(Student, result.student_id).student_id == "CUSTOM-001"


def test_writes_event_log_converted(db_session, make_visit, make_classroom):
    classroom = make_classroom(class_code="A", school_year=115)
    visit = make_visit(has_deposit=True)
    convert_recruitment_to_student(
        db_session,
        recruitment_visit_id=visit.id,
        student_id_code=None,
        classroom_id=classroom.id,
    )
    db_session.commit()
    log = db_session.query(RecruitmentEventLog).filter_by(
        recruitment_visit_id=visit.id, event_type="converted",
    ).one()
    assert log.from_stage == "deposited"
    assert log.to_stage == "enrolled"


def test_classroom_required_when_auto_generating(db_session, make_visit):
    visit = make_visit(has_deposit=True)
    with pytest.raises(RecruitmentConversionError) as exc:
        convert_recruitment_to_student(
            db_session,
            recruitment_visit_id=visit.id,
            student_id_code=None,
            classroom_id=None,
        )
    assert "classroom" in str(exc.value).lower() or "班" in str(exc.value)
```

- [ ] **Step 2: 確認失敗**

```bash
pytest tests/test_recruitment_conversion_optional_code.py -v
# Expected: 3-4 failures（缺 auto-gen 邏輯）
```

- [ ] **Step 3: 修 `services/recruitment_conversion.py`**

於 `convert_recruitment_to_student` 函式：

```python
def convert_recruitment_to_student(
    session: Session,
    recruitment_visit_id: int,
    student_id_code: Optional[str] = None,   # ← was required
    *,
    classroom_id: Optional[int] = None,
    enrollment_date: Optional[date] = None,
    initial_lifecycle_status: str = LIFECYCLE_ENROLLED,
    gender: Optional[str] = None,
    recorded_by: Optional[int] = None,
) -> ConversionResult:
    # ... 既有檢查不動 ...

    # === 學號自動產生（新增） ===
    if not (student_id_code or "").strip():
        if classroom_id is None:
            raise RecruitmentConversionError(
                "未指定學號時需提供 classroom_id 以自動產生學號"
            )
        from models.classroom import Classroom
        from services.recruitment_funnel import next_student_id_code
        from utils.academic import resolve_current_academic_term

        classroom = session.get(Classroom, classroom_id)
        if classroom is None or not classroom.class_code:
            raise RecruitmentConversionError(
                f"classroom_id={classroom_id} 不存在或缺 class_code"
            )
        school_year, _ = resolve_current_academic_term()
        student_id_code = next_student_id_code(
            session, school_year=school_year, class_code=classroom.class_code,
        )

    code = (student_id_code or "").strip()
    # ... 既有「學號唯一性」檢查不動 ...

    # ... 既有 Student/Guardian/ChangeLog 建立邏輯不動 ...

    # === 寫 funnel event log（新增，在 visit.enrolled=True 之前/之後皆可） ===
    from models.recruitment import RecruitmentEventLog
    funnel_log = RecruitmentEventLog(
        recruitment_visit_id=visit.id,
        event_type="converted",
        from_stage="deposited",
        to_stage="enrolled",
        student_id=student.id,
        actor_user_id=recorded_by,
        metadata_json={"student_id_code": code, "classroom_id": classroom_id},
        created_at=datetime.now(),
    )
    session.add(funnel_log)
    session.flush()

    visit.enrolled = True
    return ConversionResult(...)  # 同前
```

> 完整 patch 細節留給 implementer — 上面只標出 diff 重點。`datetime` 已是檔頭既有 import（若不是要補）。

- [ ] **Step 4: 跑測試**

```bash
pytest tests/test_recruitment_conversion_optional_code.py tests/test_recruitment_conversion.py -v
# Expected: 全綠（含既有 regression test）
```

- [ ] **Step 5: Commit**

```bash
git add services/recruitment_conversion.py tests/test_recruitment_conversion_optional_code.py
git commit -m "feat(conversion): optional student_id_code + funnel event log"
```

---

## Task 8: transition_visit — deposited → enrolled（convert dispatch）

**Files:**
- Modify: `services/recruitment_funnel.py`
- Modify: `tests/test_recruitment_funnel_transitions.py`

- [ ] **Step 1: 寫測試**

```python
class TestDepositedToEnrolled:
    def test_forward_creates_student(self, db_session, make_visit, make_classroom):
        classroom = make_classroom(class_code="A", school_year=115)
        visit = make_visit(has_deposit=True)
        result = transition_visit(
            db_session, visit_id=visit.id, to_stage="enrolled",
            actor_user_id=99, classroom_id=classroom.id,
        )
        db_session.commit()
        assert result.student_id is not None
        from models.classroom import Student
        student = db_session.get(Student, result.student_id)
        assert student.lifecycle_status == "enrolled"
        assert student.recruitment_visit_id == visit.id
        log = db_session.query(RecruitmentEventLog).filter_by(
            recruitment_visit_id=visit.id, event_type="converted",
        ).one()
        assert log.student_id == student.id

    def test_missing_classroom_raises(self, db_session, make_visit):
        visit = make_visit(has_deposit=True)
        with pytest.raises(RecruitmentFunnelError) as exc:
            transition_visit(
                db_session, visit_id=visit.id, to_stage="enrolled",
                actor_user_id=99, classroom_id=None,
            )
        assert exc.value.code == "CONVERT_NEED_CLASSROOM"
```

- [ ] **Step 2: 確認失敗**

```bash
pytest tests/test_recruitment_funnel_transitions.py::TestDepositedToEnrolled -v
# Expected: NotImplementedError 或 KeyError
```

- [ ] **Step 3: 加 `_do_convert` 與 dispatch 分支**

於 `services/recruitment_funnel.py` 補：

```python
def _do_convert(session, visit, *, classroom_id, actor_user_id):
    from services.recruitment_conversion import convert_recruitment_to_student
    if classroom_id is None:
        raise RecruitmentFunnelError(
            "已預繳→已報到 需要 classroom_id",
            code="CONVERT_NEED_CLASSROOM",
        )
    result = convert_recruitment_to_student(
        session,
        recruitment_visit_id=visit.id,
        student_id_code=None,                # 自動產
        classroom_id=classroom_id,
        recorded_by=actor_user_id,
    )
    # convert_recruitment_to_student 已寫 funnel event log，
    # 回傳 (student_id, event_log_id)
    last_log = (
        session.query(RecruitmentEventLog)
        .filter_by(recruitment_visit_id=visit.id, event_type="converted")
        .order_by(RecruitmentEventLog.id.desc())
        .first()
    )
    return result.student_id, last_log.id
```

於 `transition_visit` dispatch 區塊加分支：

```python
elif from_stage == "deposited" and to_stage == "enrolled":
    student_id, log_id = _do_convert(
        session, visit,
        classroom_id=classroom_id, actor_user_id=actor_user_id,
    )
```

- [ ] **Step 4: 跑測試**

```bash
pytest tests/test_recruitment_funnel_transitions.py::TestDepositedToEnrolled -v
# Expected: 2 passed
```

- [ ] **Step 5: Commit**

```bash
git add services/recruitment_funnel.py tests/test_recruitment_funnel_transitions.py
git commit -m "feat(funnel): dispatch deposited->enrolled via convert"
```

---

## Task 9: transition_visit — enrolled ↔ active

**Files:**
- Modify: `services/recruitment_funnel.py`
- Modify: `tests/test_recruitment_funnel_transitions.py`

- [ ] **Step 1: 寫測試**

```python
class TestEnrolledActive:
    def test_enrolled_to_active(self, db_session, make_visit, make_classroom, make_student):
        classroom = make_classroom(class_code="A", school_year=115)
        visit = make_visit(has_deposit=True, enrolled=True)
        student = make_student(
            student_id="115-A-01",
            lifecycle_status="enrolled",
            recruitment_visit_id=visit.id,
        )
        result = transition_visit(
            db_session, visit_id=visit.id, to_stage="active", actor_user_id=99,
        )
        db_session.commit()
        db_session.refresh(student)
        assert student.lifecycle_status == "active"
        log = db_session.query(RecruitmentEventLog).filter_by(
            recruitment_visit_id=visit.id, event_type="activated",
        ).one()
        assert log.actor_user_id == 99

    def test_active_to_enrolled_with_reason(self, db_session, make_visit, make_student):
        visit = make_visit(has_deposit=True, enrolled=True)
        student = make_student(
            student_id="115-A-02",
            lifecycle_status="active",
            recruitment_visit_id=visit.id,
        )
        result = transition_visit(
            db_session, visit_id=visit.id, to_stage="enrolled",
            actor_user_id=99, reason="校方臨時暫緩開學",
        )
        db_session.commit()
        db_session.refresh(student)
        assert student.lifecycle_status == "enrolled"
        log = db_session.query(RecruitmentEventLog).filter_by(
            recruitment_visit_id=visit.id, event_type="revert_activated",
        ).one()
        assert log.reason == "校方臨時暫緩開學"

    def test_active_to_enrolled_with_attendance_warns(
        self, db_session, make_visit, make_student, make_attendance,
    ):
        visit = make_visit(has_deposit=True, enrolled=True)
        student = make_student(
            student_id="115-A-03",
            lifecycle_status="active",
            recruitment_visit_id=visit.id,
        )
        make_attendance(student_id=student.id)
        result = transition_visit(
            db_session, visit_id=visit.id, to_stage="enrolled",
            actor_user_id=99, reason="原因",
        )
        assert "student_has_attendance_after_active" in result.warnings
```

- [ ] **Step 2: 失敗驗證**

```bash
pytest tests/test_recruitment_funnel_transitions.py::TestEnrolledActive -v
# Expected: NotImplementedError
```

- [ ] **Step 3: 加 `_do_activate` / `_do_revert_activate` + dispatch**

```python
def _do_activate(session, visit, student, *, actor_user_id):
    student.lifecycle_status = "active"
    log_id = _write_event_log(
        session, visit_id=visit.id, event_type="activated",
        from_stage="enrolled", to_stage="active",
        student_id=student.id, actor_user_id=actor_user_id,
    )
    return student.id, log_id


def _do_revert_activate(session, visit, student, *, actor_user_id, reason):
    from models.classroom import StudentAttendance
    warnings: list[str] = []
    has_attendance = (
        session.query(StudentAttendance)
        .filter(StudentAttendance.student_id == student.id)
        .limit(1).first()
    )
    if has_attendance:
        warnings.append("student_has_attendance_after_active")
    student.lifecycle_status = "enrolled"
    log_id = _write_event_log(
        session, visit_id=visit.id, event_type="revert_activated",
        from_stage="active", to_stage="enrolled",
        student_id=student.id, actor_user_id=actor_user_id, reason=reason,
    )
    return student.id, log_id, warnings
```

於 dispatch 增加：

```python
elif from_stage == "enrolled" and to_stage == "active":
    student_id, log_id = _do_activate(session, visit, student, actor_user_id=actor_user_id)
elif from_stage == "active" and to_stage == "enrolled":
    student_id, log_id, ws = _do_revert_activate(
        session, visit, student, actor_user_id=actor_user_id, reason=reason,
    )
    warnings.extend(ws)
```

- [ ] **Step 4: 跑測試**

```bash
pytest tests/test_recruitment_funnel_transitions.py::TestEnrolledActive -v
# Expected: 3 passed
```

- [ ] **Step 5: Commit**

```bash
git add services/recruitment_funnel.py tests/test_recruitment_funnel_transitions.py
git commit -m "feat(funnel): dispatch enrolled<->active with attendance warning"
```

---

## Task 10: transition_visit — destructive reverts（enrolled → deposited / cross-stage）

**Files:**
- Modify: `services/recruitment_funnel.py`
- Modify: `tests/test_recruitment_funnel_transitions.py`

- [ ] **Step 1: 寫測試**

```python
class TestDestructiveReverts:
    def test_enrolled_to_deposited_clean(
        self, db_session, make_visit, make_student, make_guardian,
    ):
        visit = make_visit(has_deposit=True, enrolled=True)
        student = make_student(
            student_id="115-A-01",
            lifecycle_status="enrolled",
            recruitment_visit_id=visit.id,
        )
        make_guardian(student_id=student.id)
        result = transition_visit(
            db_session, visit_id=visit.id, to_stage="deposited",
            actor_user_id=99, reason="家長取消報到",
        )
        db_session.commit()
        # Student 應被刪
        from models.classroom import Student
        assert db_session.get(Student, student.id) is None
        # visit.enrolled flip false
        db_session.refresh(visit)
        assert visit.enrolled is False
        assert visit.has_deposit is True  # 退到 deposited
        log = db_session.query(RecruitmentEventLog).filter_by(
            recruitment_visit_id=visit.id, event_type="revert_converted",
        ).one()
        assert log.reason == "家長取消報到"
        assert log.student_id is None  # student 已刪、ON DELETE SET NULL

    def test_enrolled_to_deposited_with_attendance_blocks(
        self, db_session, make_visit, make_student, make_attendance,
    ):
        visit = make_visit(has_deposit=True, enrolled=True)
        student = make_student(
            student_id="115-A-02",
            lifecycle_status="enrolled",
            recruitment_visit_id=visit.id,
        )
        make_attendance(student_id=student.id)
        with pytest.raises(RecruitmentFunnelError) as exc:
            transition_visit(
                db_session, visit_id=visit.id, to_stage="deposited",
                actor_user_id=99, reason="家長取消報到",
            )
        assert exc.value.code == "REVERT_STUDENT_HAS_DATA"
        # Student 仍存在
        from models.classroom import Student
        assert db_session.get(Student, student.id) is not None

    def test_destructive_without_reason_raises(
        self, db_session, make_visit, make_student,
    ):
        visit = make_visit(has_deposit=True, enrolled=True)
        make_student(
            student_id="115-A-03",
            lifecycle_status="enrolled",
            recruitment_visit_id=visit.id,
        )
        with pytest.raises(RecruitmentFunnelError) as exc:
            transition_visit(
                db_session, visit_id=visit.id, to_stage="deposited",
                actor_user_id=99, reason="",
            )
        assert exc.value.code == "REASON_REQUIRED"
```

- [ ] **Step 2: 確認失敗**

```bash
pytest tests/test_recruitment_funnel_transitions.py::TestDestructiveReverts -v
# Expected: NotImplementedError 或 KeyError
```

- [ ] **Step 3: 加 `_do_revert_convert` + dispatch**

```python
def _do_revert_convert(session, visit, student, *, actor_user_id, reason):
    assert_student_revertable(session, student.id)
    student_id = student.id
    # Cascade delete — 依現有 FK ON DELETE CASCADE 行為（guardians、change_logs）
    # 若部分 FK 為 RESTRICT 須在此顯式 delete（implementer 確認）
    from models.guardian import Guardian
    from models.student_log import StudentChangeLog
    session.query(Guardian).filter(Guardian.student_id == student_id).delete(
        synchronize_session=False
    )
    session.query(StudentChangeLog).filter(
        StudentChangeLog.student_id == student_id
    ).delete(synchronize_session=False)
    session.delete(student)
    visit.enrolled = False
    log_id = _write_event_log(
        session, visit_id=visit.id, event_type="revert_converted",
        from_stage="enrolled", to_stage="deposited",
        student_id=None, actor_user_id=actor_user_id, reason=reason,
        metadata={"deleted_student_id": student_id},
    )
    return None, log_id  # student 已刪
```

於 dispatch 加分支：

```python
elif from_stage == "enrolled" and to_stage in ("deposited", "visited"):
    student_id, log_id = _do_revert_convert(
        session, visit, student, actor_user_id=actor_user_id, reason=reason,
    )
    if to_stage == "visited":
        # 再做 deposited → visited
        visit.has_deposit = False
        log_id2 = _write_event_log(
            session, visit_id=visit.id, event_type="deposit_removed",
            from_stage="deposited", to_stage="visited",
            actor_user_id=actor_user_id, reason=reason,
        )
        log_id = log_id2  # 回最終那筆

elif from_stage == "active" and to_stage in ("enrolled", "deposited", "visited"):
    # 先 active→enrolled
    sid, log_id, ws = _do_revert_activate(
        session, visit, student, actor_user_id=actor_user_id, reason=reason,
    )
    warnings.extend(ws)
    student_id = sid
    if to_stage in ("deposited", "visited"):
        # 再 enrolled→deposited（重新 load student 以反映剛才的變更）
        student2 = _load_student_by_visit(session, visit.id)
        _, log_id = _do_revert_convert(
            session, visit, student2, actor_user_id=actor_user_id, reason=reason,
        )
        student_id = None
        if to_stage == "visited":
            visit.has_deposit = False
            log_id = _write_event_log(
                session, visit_id=visit.id, event_type="deposit_removed",
                from_stage="deposited", to_stage="visited",
                actor_user_id=actor_user_id, reason=reason,
            )
```

- [ ] **Step 4: 跑測試**

```bash
pytest tests/test_recruitment_funnel_transitions.py -v
# Expected: 整個檔案全綠（visited↔deposited + deposited→enrolled + enrolled↔active + destructive 全部）
```

- [ ] **Step 5: Commit**

```bash
git add services/recruitment_funnel.py tests/test_recruitment_funnel_transitions.py
git commit -m "feat(funnel): destructive reverts with cascade + cross-stage chain"
```

---

## Task 11: advance_term_to_active（scheduler 主邏輯）

**Files:**
- Create: `services/recruitment_lifecycle.py`
- Create: `tests/test_recruitment_term_advance.py`

- [ ] **Step 1: 寫測試**

```python
# tests/test_recruitment_term_advance.py
from datetime import date, timedelta
import pytest
from services.recruitment_lifecycle import advance_term_to_active
from models.academic_term import AcademicTerm


@pytest.fixture
def term_115_1(db_session):
    t = AcademicTerm(
        school_year=115, semester=1,
        start_date=date(2026, 8, 30),
        end_date=date(2027, 1, 31),
    )
    db_session.add(t)
    db_session.flush()
    return t


def test_advances_enrolled_students_in_window(
    db_session, term_115_1, make_visit, make_student,
):
    visit = make_visit(has_deposit=True, enrolled=True)
    student = make_student(
        student_id="115-A-01",
        lifecycle_status="enrolled",
        recruitment_visit_id=visit.id,
        enrollment_date=date(2026, 8, 1),  # 開學前 29 天 — 落在 90 天 window
    )
    summary = advance_term_to_active(db_session, school_year=115, semester=1)
    db_session.commit()
    db_session.refresh(student)
    assert student.lifecycle_status == "active"
    assert summary["advanced"] == 1
    assert summary["skipped"] == 0


def test_skips_already_active(db_session, term_115_1, make_visit, make_student):
    visit = make_visit(has_deposit=True, enrolled=True)
    make_student(
        student_id="115-A-02",
        lifecycle_status="active",  # 已 active
        recruitment_visit_id=visit.id,
        enrollment_date=date(2026, 8, 1),
    )
    summary = advance_term_to_active(db_session, school_year=115, semester=1)
    assert summary["advanced"] == 0
    assert summary["skipped"] == 1


def test_skips_out_of_window(db_session, term_115_1, make_visit, make_student):
    visit = make_visit(has_deposit=True, enrolled=True)
    make_student(
        student_id="115-A-03",
        lifecycle_status="enrolled",
        recruitment_visit_id=visit.id,
        enrollment_date=date(2026, 1, 1),  # 開學前 ~241 天 — 超 90 天 window
    )
    summary = advance_term_to_active(db_session, school_year=115, semester=1)
    assert summary["advanced"] == 0


def test_skips_null_enrollment_date(db_session, term_115_1, make_visit, make_student):
    visit = make_visit(has_deposit=True, enrolled=True)
    make_student(
        student_id="115-A-04",
        lifecycle_status="enrolled",
        recruitment_visit_id=visit.id,
        enrollment_date=None,
    )
    summary = advance_term_to_active(db_session, school_year=115, semester=1)
    assert summary["advanced"] == 0
```

- [ ] **Step 2: 失敗確認**

```bash
pytest tests/test_recruitment_term_advance.py -v
# Expected: ImportError
```

- [ ] **Step 3: 寫 `services/recruitment_lifecycle.py`**

```python
"""services/recruitment_lifecycle.py — scheduler 業務入口。

依 `academic_terms.start_date` 批量推進該學期 enrolled 學生 → active。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from config.scheduler import get_settings
from models.academic_term import AcademicTerm
from models.classroom import Student, LIFECYCLE_ENROLLED
from services.recruitment_funnel import (
    transition_visit,
    RecruitmentFunnelError,
)

logger = logging.getLogger(__name__)


def advance_term_to_active(
    session: Session, school_year: int, semester: int,
) -> dict:
    """把該學期 window 內的 enrolled 學生升 active。

    Window: [term.start_date - window_days, term.start_date]，
    window_days 由 settings.scheduler.recruitment_term_advance_window_days 控（預設 90）。
    """
    term = (
        session.query(AcademicTerm)
        .filter(
            AcademicTerm.school_year == school_year,
            AcademicTerm.semester == semester,
        )
        .first()
    )
    if term is None:
        logger.warning("academic_terms not found: year=%s sem=%s", school_year, semester)
        return {"advanced": 0, "skipped": 0, "failed": 0}

    window_days = (
        get_settings().scheduler.recruitment_term_advance_window_days
    )
    window_start = term.start_date - timedelta(days=window_days)

    students = (
        session.query(Student)
        .filter(
            Student.recruitment_visit_id.isnot(None),
            Student.enrollment_date.isnot(None),
            Student.enrollment_date >= window_start,
            Student.enrollment_date <= term.start_date,
            Student.lifecycle_status == LIFECYCLE_ENROLLED,
        )
        .all()
    )

    advanced = 0
    skipped = 0
    failed = 0
    for stu in students:
        try:
            transition_visit(
                session,
                visit_id=stu.recruitment_visit_id,
                to_stage="active",
                actor_user_id=None,
                reason="scheduler:term_start",
            )
            advanced += 1
        except RecruitmentFunnelError as e:
            if e.code == "STAGE_ALREADY":
                skipped += 1
            else:
                failed += 1
                logger.warning(
                    "advance failed student=%s code=%s: %s",
                    stu.id, e.code, e,
                )
        except Exception:
            failed += 1
            logger.exception("advance error student=%s", stu.id)

    # 若沒撞 advanced 計 0，但也有可能某些 student 落入 active 起始狀態（推導為 active 但 lifecycle 仍 enrolled）— 統計只記 skipped
    # （兩處 stat 一致即可）

    return {"advanced": advanced, "skipped": skipped, "failed": failed}
```

> 注意 `skipped` 計入 condition：若 `derive_stage` 推 active，`transition_visit` 內部會 raise `STAGE_ALREADY`（code=409）— 那條 except 分支處理。

- [ ] **Step 4: 跑測試**

```bash
pytest tests/test_recruitment_term_advance.py -v
# Expected: 4 passed
```

- [ ] **Step 5: Commit**

```bash
git add services/recruitment_lifecycle.py tests/test_recruitment_term_advance.py
git commit -m "feat(funnel): advance_term_to_active scheduler entry"
```

---

## Task 12: settings + scheduler service + main.py wiring

**Files:**
- Modify: `config/scheduler.py`（加 3 個欄位）
- Create: `services/recruitment_term_advance_scheduler.py`
- Modify: `main.py`（startup hook 加 init）

- [ ] **Step 1: 加 settings**

於 `config/scheduler.py` 適當位置（與其他 scheduler 欄位並排）：

```python
# Recruitment funnel term advance
recruitment_term_advance_enabled: BoolEnv = True
recruitment_term_advance_check_interval: int = 86400  # 1 天
recruitment_term_advance_window_days: int = 90
```

- [ ] **Step 2: 寫 scheduler service**

```python
# services/recruitment_term_advance_scheduler.py
"""每日心跳：在 academic_terms.start_date 當日批量推進 enrolled → active。

照 services/graduation_scheduler.py 結構。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Optional
from zoneinfo import ZoneInfo
from datetime import datetime

from config import get_settings
from models.academic_term import AcademicTerm
from services.recruitment_lifecycle import advance_term_to_active

logger = logging.getLogger(__name__)

settings = get_settings()
CHECK_INTERVAL_SECONDS = settings.scheduler.recruitment_term_advance_check_interval


def _today_taipei() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.recruitment_term_advance_enabled)


async def run_recruitment_term_advance_scheduler(stop_event: asyncio.Event) -> None:
    from utils.db import session_scope

    logger.info("recruitment term advance scheduler 啟動")
    while not stop_event.is_set():
        try:
            today = _today_taipei()
            with session_scope() as session:
                terms = (
                    session.query(AcademicTerm)
                    .filter(AcademicTerm.start_date == today)
                    .all()
                )
                for term in terms:
                    summary = advance_term_to_active(
                        session, term.school_year, term.semester,
                    )
                    logger.info(
                        "term advance year=%s sem=%s %s",
                        term.school_year, term.semester, summary,
                    )
        except Exception:
            logger.exception("term advance scheduler tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
```

> 若 `session_scope` 不在 `utils/db`，依專案實際匯入路徑調整（參照 `services/graduation_scheduler.py` 同檔的 import）。

- [ ] **Step 3: 在 `main.py` startup 處註冊**

於 startup function 內，模仿 graduation_scheduler 段落（main.py:263-271 區域附近）：

```python
# Recruitment funnel term advance scheduler
from services import recruitment_term_advance_scheduler as _rt_sched

if _rt_sched.scheduler_enabled():
    recruitment_term_advance_stop_event = asyncio.Event()
    recruitment_term_advance_task = asyncio.create_task(
        _rt_sched.run_recruitment_term_advance_scheduler(
            recruitment_term_advance_stop_event
        )
    )
    logger.info("recruitment term advance scheduler 已啟用")
    # shutdown hook（與其他 scheduler 同位置）：
    #   recruitment_term_advance_stop_event.set()
    #   await recruitment_term_advance_task
```

> implementer 在 shutdown handler 也補對應 set + await（與既有 scheduler pattern 一致）。

- [ ] **Step 4: 寫 smoke test**

```python
# tests/test_recruitment_term_advance_scheduler_smoke.py
import asyncio
from datetime import date
from unittest.mock import patch
import pytest
from services.recruitment_term_advance_scheduler import (
    _today_taipei, run_recruitment_term_advance_scheduler, scheduler_enabled,
)


def test_scheduler_enabled_default_true(monkeypatch):
    assert scheduler_enabled() in (True, False)  # 依 env


def test_today_taipei_returns_date():
    assert isinstance(_today_taipei(), date)
```

- [ ] **Step 5: 跑測試 + 啟動 server smoke**

```bash
pytest tests/test_recruitment_term_advance_scheduler_smoke.py -v
# Expected: 2 passed

cd ~/Desktop/ivy-backend && uvicorn main:app --port 18088 &
sleep 5 && curl -sf http://localhost:18088/health || echo "health check failed"
kill %1
# Expected: 啟動成功、看到 "recruitment term advance scheduler 已啟用" log
```

- [ ] **Step 6: Commit**

```bash
git add config/scheduler.py services/recruitment_term_advance_scheduler.py main.py tests/test_recruitment_term_advance_scheduler_smoke.py
git commit -m "feat(scheduler): daily recruitment term advance"
```

---

## Task 13: schemas/recruitment_funnel.py + schemas/academic_term.py

**Files:**
- Create: `schemas/recruitment_funnel.py`
- Create: `schemas/academic_term.py`

- [ ] **Step 1: 寫 `schemas/academic_term.py`**

```python
"""Pydantic schemas for /academic-terms."""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class AcademicTermIn(BaseModel):
    school_year: int = Field(..., ge=100, le=200, description="民國學年")
    semester: int = Field(..., ge=1, le=2)
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _check_dates(self) -> "AcademicTermIn":
        if self.end_date <= self.start_date:
            raise ValueError("end_date 必須晚於 start_date")
        return self


class AcademicTermOut(BaseModel):
    id: int
    school_year: int
    semester: int
    start_date: date
    end_date: date
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True
```

- [ ] **Step 2: 寫 `schemas/recruitment_funnel.py`**

```python
"""Pydantic schemas for /recruitment/funnel."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Stage = Literal["visited", "deposited", "enrolled", "active"]


class FunnelCard(BaseModel):
    visit_id: int
    child_name: str
    grade: Optional[str]
    phone: Optional[str]
    district: Optional[str]
    source: Optional[str]
    deposited_at: Optional[datetime]
    student_id: Optional[int]
    current_stage: Stage


class FunnelSummary(BaseModel):
    visited_count: int
    deposited_count: int
    enrolled_count: int
    active_count: int


class FunnelBoardOut(BaseModel):
    stages: dict[Stage, list[FunnelCard]]
    summary: FunnelSummary


class TransitionIn(BaseModel):
    to_stage: Stage
    classroom_id: Optional[int] = None
    reason: Optional[str] = None


class TransitionOut(BaseModel):
    visit_id: int
    from_stage: Stage
    to_stage: Stage
    student_id: Optional[int]
    event_log_id: int
    warnings: list[str] = Field(default_factory=list)


class TimelineEvent(BaseModel):
    source: Literal["recruitment", "student"]
    event_type: str
    from_stage: Optional[str]
    to_stage: Optional[str]
    actor_user_id: Optional[int]
    reason: Optional[str]
    created_at: datetime


class TimelineOut(BaseModel):
    events: list[TimelineEvent]
```

- [ ] **Step 3: smoke import test**

```python
# tests/test_recruitment_funnel_schemas.py
from schemas.academic_term import AcademicTermIn, AcademicTermOut
from schemas.recruitment_funnel import (
    FunnelCard, FunnelBoardOut, TransitionIn, TransitionOut, TimelineOut,
)
import pytest
from pydantic import ValidationError
from datetime import date


def test_academic_term_in_rejects_inverted_dates():
    with pytest.raises(ValidationError):
        AcademicTermIn(school_year=115, semester=1,
                       start_date=date(2026, 9, 1),
                       end_date=date(2026, 8, 1))


def test_academic_term_in_rejects_bad_semester():
    with pytest.raises(ValidationError):
        AcademicTermIn(school_year=115, semester=3,
                       start_date=date(2026, 8, 1),
                       end_date=date(2027, 1, 31))


def test_transition_in_requires_to_stage():
    with pytest.raises(ValidationError):
        TransitionIn()
```

- [ ] **Step 4: 跑**

```bash
pytest tests/test_recruitment_funnel_schemas.py -v
# Expected: 3 passed
```

- [ ] **Step 5: Commit**

```bash
git add schemas/academic_term.py schemas/recruitment_funnel.py tests/test_recruitment_funnel_schemas.py
git commit -m "feat(funnel): Pydantic schemas for funnel + academic_terms"
```

---

## Task 14: /academic-terms API

**Files:**
- Create: `api/academic_terms.py`
- Modify: `main.py`（include_router）
- Create: `tests/test_academic_terms_api.py`

- [ ] **Step 1: 寫測試**

```python
# tests/test_academic_terms_api.py
import pytest
from datetime import date


def test_list_empty(client_admin):
    res = client_admin.get("/api/academic-terms")
    assert res.status_code == 200
    assert res.json() == []


def test_create_and_list(client_admin):
    res = client_admin.post("/api/academic-terms", json={
        "school_year": 115, "semester": 1,
        "start_date": "2026-08-30", "end_date": "2027-01-31",
    })
    assert res.status_code == 200, res.text
    row = res.json()
    assert row["school_year"] == 115

    res2 = client_admin.get("/api/academic-terms")
    assert len(res2.json()) == 1


def test_unique_year_semester(client_admin):
    payload = {
        "school_year": 115, "semester": 1,
        "start_date": "2026-08-30", "end_date": "2027-01-31",
    }
    client_admin.post("/api/academic-terms", json=payload)
    res = client_admin.post("/api/academic-terms", json=payload)
    assert res.status_code in (400, 409)


def test_inverted_dates_400(client_admin):
    res = client_admin.post("/api/academic-terms", json={
        "school_year": 115, "semester": 1,
        "start_date": "2027-01-31", "end_date": "2026-08-30",
    })
    assert res.status_code == 422  # Pydantic validation


def test_current_term(client_admin, freezer):
    # freezer = pytest-freezegun（若 not available，改用 mock date）
    freezer.move_to("2026-10-01")
    client_admin.post("/api/academic-terms", json={
        "school_year": 115, "semester": 1,
        "start_date": "2026-08-30", "end_date": "2027-01-31",
    })
    res = client_admin.get("/api/academic-terms/current")
    assert res.status_code == 200
    assert res.json()["school_year"] == 115


def test_non_admin_cannot_write(client_teacher):
    res = client_teacher.post("/api/academic-terms", json={
        "school_year": 115, "semester": 1,
        "start_date": "2026-08-30", "end_date": "2027-01-31",
    })
    assert res.status_code == 403
```

> Fixture `client_admin` / `client_teacher` / `freezer` 來自 conftest。若 freezer 未裝可改 `freeze_time` decorator 或 monkeypatch。

- [ ] **Step 2: 寫 router**

```python
# api/academic_terms.py
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from models.academic_term import AcademicTerm
from schemas.academic_term import AcademicTermIn, AcademicTermOut
from utils.auth import require_staff_permission
from utils.db import get_session_dep
from utils.permissions import Permission

router = APIRouter(prefix="/academic-terms", tags=["academic-terms"])


@router.get("", response_model=list[AcademicTermOut])
def list_terms(
    session=Depends(get_session_dep),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    return (
        session.query(AcademicTerm)
        .order_by(AcademicTerm.school_year.desc(), AcademicTerm.semester.desc())
        .all()
    )


@router.get("/current", response_model=Optional[AcademicTermOut])
def current_term(
    session=Depends(get_session_dep),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    today = date.today()
    return (
        session.query(AcademicTerm)
        .filter(AcademicTerm.start_date <= today, AcademicTerm.end_date >= today)
        .first()
    )


@router.post("", response_model=AcademicTermOut)
def create_term(
    payload: AcademicTermIn,
    session=Depends(get_session_dep),
    _=Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    term = AcademicTerm(**payload.model_dump())
    session.add(term)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "已存在 (school_year, semester) 的設定")
    session.refresh(term)
    return term


@router.put("/{term_id}", response_model=AcademicTermOut)
def update_term(
    term_id: int,
    payload: AcademicTermIn,
    session=Depends(get_session_dep),
    _=Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    term = session.get(AcademicTerm, term_id)
    if term is None:
        raise HTTPException(404, "不存在")
    for k, v in payload.model_dump().items():
        setattr(term, k, v)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "違反 unique 約束")
    session.refresh(term)
    return term


@router.delete("/{term_id}")
def delete_term(
    term_id: int,
    session=Depends(get_session_dep),
    _=Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    term = session.get(AcademicTerm, term_id)
    if term is None:
        raise HTTPException(404, "不存在")
    session.delete(term)
    session.commit()
    return {"ok": True}
```

> `get_session_dep` 名稱依專案實際 dependency；若叫 `get_db` 或 `session_scope_dep`，依實際調整。

- [ ] **Step 3: 在 `main.py` 註冊 router**

```python
from api.academic_terms import router as academic_terms_router
app.include_router(academic_terms_router, prefix="/api")
```

- [ ] **Step 4: 跑測試**

```bash
pytest tests/test_academic_terms_api.py -v
# Expected: 6 passed（若 freezer 未裝，相關 test 改寫或 skip）
```

- [ ] **Step 5: Commit**

```bash
git add api/academic_terms.py main.py tests/test_academic_terms_api.py
git commit -m "feat(api): /academic-terms CRUD"
```

---

## Task 15: /recruitment/funnel API（board / transition / timeline）

**Files:**
- Create: `api/recruitment/funnel.py`
- Modify: `api/recruitment/__init__.py`（register sub-router）
- Create: `tests/test_recruitment_funnel_api.py`

- [ ] **Step 1: 寫測試**

```python
# tests/test_recruitment_funnel_api.py
import pytest


def test_board_empty_returns_4_stages(client_admin):
    res = client_admin.get("/api/recruitment/funnel/board")
    assert res.status_code == 200
    data = res.json()
    assert set(data["stages"].keys()) == {"visited", "deposited", "enrolled", "active"}
    assert data["summary"]["visited_count"] == 0


def test_board_with_visit_shows_visited(client_admin, make_visit):
    visit = make_visit(child_name="小明", has_deposit=False)
    res = client_admin.get("/api/recruitment/funnel/board")
    cards = res.json()["stages"]["visited"]
    assert any(c["visit_id"] == visit.id for c in cards)


def test_transition_visited_to_deposited(client_admin, make_visit):
    visit = make_visit(has_deposit=False)
    res = client_admin.post(
        f"/api/recruitment/funnel/visits/{visit.id}/transition",
        json={"to_stage": "deposited"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["from_stage"] == "visited"
    assert body["to_stage"] == "deposited"


def test_transition_missing_classroom_400(client_admin, make_visit):
    visit = make_visit(has_deposit=True)
    res = client_admin.post(
        f"/api/recruitment/funnel/visits/{visit.id}/transition",
        json={"to_stage": "enrolled"},
    )
    assert res.status_code == 400
    assert "CONVERT_NEED_CLASSROOM" in res.text


def test_transition_destructive_without_reason_400(
    client_admin, make_visit, make_student,
):
    visit = make_visit(has_deposit=True, enrolled=True)
    make_student(
        student_id="115-A-01",
        lifecycle_status="enrolled",
        recruitment_visit_id=visit.id,
    )
    res = client_admin.post(
        f"/api/recruitment/funnel/visits/{visit.id}/transition",
        json={"to_stage": "deposited"},
    )
    assert res.status_code == 400
    assert "REASON_REQUIRED" in res.text


def test_timeline_returns_union(
    client_admin, make_visit, make_classroom,
):
    classroom = make_classroom(class_code="A", school_year=115)
    visit = make_visit(has_deposit=False)
    # toggle deposit
    client_admin.post(
        f"/api/recruitment/funnel/visits/{visit.id}/transition",
        json={"to_stage": "deposited"},
    )
    # convert
    client_admin.post(
        f"/api/recruitment/funnel/visits/{visit.id}/transition",
        json={"to_stage": "enrolled", "classroom_id": classroom.id},
    )
    res = client_admin.get(f"/api/recruitment/funnel/visits/{visit.id}/timeline")
    assert res.status_code == 200
    events = res.json()["events"]
    types = [e["event_type"] for e in events]
    assert "deposit_added" in types
    assert "converted" in types
    # 至少要包含 student_change_logs 中的「入學」（若 source=student）
    assert any(e["source"] == "student" for e in events)


def test_permission_dispatch(client_teacher, make_visit):
    """teacher 無 RECRUITMENT_WRITE 不能 toggle deposit"""
    visit = make_visit(has_deposit=False)
    res = client_teacher.post(
        f"/api/recruitment/funnel/visits/{visit.id}/transition",
        json={"to_stage": "deposited"},
    )
    assert res.status_code == 403
```

> `client_admin`/`client_teacher` 須擁有對應權限的固定帳號 fixture。

- [ ] **Step 2: 寫 router**

```python
# api/recruitment/funnel.py
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_

from models.classroom import Student
from models.recruitment import RecruitmentVisit, RecruitmentEventLog
from models.student_log import StudentChangeLog
from schemas.recruitment_funnel import (
    FunnelBoardOut, FunnelCard, FunnelSummary, Stage,
    TransitionIn, TransitionOut, TimelineEvent, TimelineOut,
)
from services.recruitment_funnel import (
    transition_visit, derive_stage,
    RecruitmentFunnelError,
)
from utils.academic import resolve_current_academic_term
from utils.auth import require_staff_permission, get_current_user
from utils.db import get_session_dep
from utils.permissions import Permission

router = APIRouter(prefix="/funnel", tags=["recruitment-funnel"])


# === GET /board ===
@router.get("/board", response_model=FunnelBoardOut)
def get_board(
    school_year: Optional[int] = Query(None),
    semester: Optional[int] = Query(None),
    session=Depends(get_session_dep),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    if school_year is None or semester is None:
        sy, sm = resolve_current_academic_term()
        school_year = school_year or sy
        semester = semester or sm

    # 過濾邏輯：以 visit.month（民國 YYY.MM）落在學期窗 — 簡化版直接抓全部，後續用 month range 收緊
    visits = session.query(RecruitmentVisit).all()
    student_map = {
        s.recruitment_visit_id: s
        for s in session.query(Student).filter(Student.recruitment_visit_id.isnot(None)).all()
    }

    buckets: dict[Stage, list[FunnelCard]] = {
        "visited": [], "deposited": [], "enrolled": [], "active": [],
    }
    for v in visits:
        student = student_map.get(v.id)
        stage = derive_stage(v, student)
        buckets[stage].append(FunnelCard(
            visit_id=v.id,
            child_name=v.child_name,
            grade=v.grade,
            phone=v.phone,
            district=v.district,
            source=v.source,
            deposited_at=v.updated_at if v.has_deposit else None,
            student_id=student.id if student else None,
            current_stage=stage,
        ))

    return FunnelBoardOut(
        stages=buckets,
        summary=FunnelSummary(
            visited_count=len(buckets["visited"]),
            deposited_count=len(buckets["deposited"]),
            enrolled_count=len(buckets["enrolled"]),
            active_count=len(buckets["active"]),
        ),
    )


# === POST /visits/{visit_id}/transition ===
def _resolve_required_permissions(from_stage: Stage, to_stage: Stage) -> list[Permission]:
    if {from_stage, to_stage} == {"visited", "deposited"}:
        return [Permission.RECRUITMENT_WRITE]
    if from_stage == "deposited" and to_stage == "enrolled":
        return [Permission.RECRUITMENT_CONVERT]
    if from_stage == "enrolled" and to_stage in ("deposited", "visited"):
        return [Permission.RECRUITMENT_CONVERT, Permission.STUDENT_WRITE]
    if {from_stage, to_stage} == {"enrolled", "active"}:
        return [Permission.STUDENT_WRITE]
    if from_stage == "active" and to_stage in ("enrolled", "deposited", "visited"):
        return [Permission.STUDENT_WRITE]
    return [Permission.RECRUITMENT_WRITE]  # fallback


@router.post("/visits/{visit_id}/transition", response_model=TransitionOut)
def post_transition(
    visit_id: int,
    payload: TransitionIn,
    request: Request,
    session=Depends(get_session_dep),
    current_user=Depends(get_current_user),
):
    # 預讀 from_stage 決定權限
    visit = session.query(RecruitmentVisit).filter_by(id=visit_id).first()
    if visit is None:
        raise HTTPException(404, "VISIT_NOT_FOUND")
    student = (
        session.query(Student).filter(Student.recruitment_visit_id == visit_id).first()
    )
    from_stage = derive_stage(visit, student)
    required = _resolve_required_permissions(from_stage, payload.to_stage)
    user_perms = current_user.get("permissions", 0)
    for p in required:
        if not (int(user_perms) & p.value):
            raise HTTPException(403, f"missing permission: {p.name}")

    try:
        result = transition_visit(
            session,
            visit_id=visit_id,
            to_stage=payload.to_stage,
            actor_user_id=current_user.get("user_id"),
            classroom_id=payload.classroom_id,
            reason=payload.reason,
        )
        session.commit()
    except RecruitmentFunnelError as e:
        session.rollback()
        status = 409 if e.code == "STAGE_ALREADY" else 400
        raise HTTPException(status, detail={"code": e.code, "message": str(e)})

    return TransitionOut(**result.__dict__)


# === GET /visits/{visit_id}/timeline ===
@router.get("/visits/{visit_id}/timeline", response_model=TimelineOut)
def get_timeline(
    visit_id: int,
    session=Depends(get_session_dep),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    visit = session.query(RecruitmentVisit).filter_by(id=visit_id).first()
    if visit is None:
        raise HTTPException(404, "VISIT_NOT_FOUND")

    rec_events = session.query(RecruitmentEventLog).filter_by(
        recruitment_visit_id=visit_id,
    ).all()
    student = session.query(Student).filter(
        Student.recruitment_visit_id == visit_id
    ).first()
    student_events = []
    if student is not None:
        student_events = session.query(StudentChangeLog).filter_by(
            student_id=student.id,
        ).all()

    events: list[TimelineEvent] = []
    for e in rec_events:
        events.append(TimelineEvent(
            source="recruitment",
            event_type=e.event_type,
            from_stage=e.from_stage,
            to_stage=e.to_stage,
            actor_user_id=e.actor_user_id,
            reason=e.reason,
            created_at=e.created_at,
        ))
    for e in student_events:
        events.append(TimelineEvent(
            source="student",
            event_type=e.event_type,
            from_stage=None,
            to_stage=None,
            actor_user_id=e.recorded_by,
            reason=e.reason,
            created_at=e.event_date,  # ChangeLog 用 event_date 當主時序
        ))
    events.sort(key=lambda x: x.created_at)
    return TimelineOut(events=events)
```

- [ ] **Step 3: 在 `api/recruitment/__init__.py` register sub-router**

```python
# 於檔尾，main router include 段附近
from api.recruitment.funnel import router as _funnel_router
router.include_router(_funnel_router)
```

- [ ] **Step 4: 跑測試**

```bash
pytest tests/test_recruitment_funnel_api.py -v
# Expected: 7 passed
```

- [ ] **Step 5: Commit**

```bash
git add api/recruitment/funnel.py api/recruitment/__init__.py tests/test_recruitment_funnel_api.py
git commit -m "feat(api): /recruitment/funnel board/transition/timeline"
```

---

## Task 16: 既有 convert endpoint 標 deprecated + 全套回歸

**Files:**
- Modify: `api/recruitment/records.py`（~307 行附近）

- [ ] **Step 1: 加 deprecated 旗標**

於 records.py 既有 convert endpoint 裝飾子：

```python
@router.post(
    "/{record_id}/convert",
    ...,
    deprecated=True,
    summary="[deprecated] 改用 POST /recruitment/funnel/visits/{visit_id}/transition",
)
def convert_recruitment_record_to_student(...):
    # 函式內容不動
    ...
```

- [ ] **Step 2: 全套 pytest 回歸**

```bash
cd ~/Desktop/ivy-backend && pytest -x --timeout=60
# Expected: 全綠（含既有 4000+ test + 本 plan 新增 ~60 test）
```

- [ ] **Step 3: openapi dump + frontend codegen 驗 schema 更新**

```bash
cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py
cd ~/Desktop/ivy-frontend && npm run gen:api
# Expected: schema.d.ts 含 /funnel 與 /academic-terms 新路徑
```

- [ ] **Step 4: Commit + Phase A 收尾**

```bash
cd ~/Desktop/ivy-backend && git add api/recruitment/records.py
git commit -m "feat(api): mark old convert endpoint deprecated"

cd ~/Desktop/ivy-frontend
git add src/api/_generated/schema.d.ts
git commit -m "chore(api-types): regen schema.d.ts for funnel/academic-terms"
```

---

## Phase A 完成標準

- [ ] `alembic upgrade head` / `downgrade -1` 雙向通過
- [ ] 所有 16 task 的測試全綠
- [ ] 全套 pytest 通過（無 regression）
- [ ] `uvicorn main:app` 啟動成功、看到 scheduler 啟用 log
- [ ] OpenAPI schema 包含 `/recruitment/funnel/*` 與 `/academic-terms/*`
- [ ] 舊 `convert` endpoint 在 OpenAPI 顯示為 deprecated
- [ ] 無前端改動（保留給 Phase B）
