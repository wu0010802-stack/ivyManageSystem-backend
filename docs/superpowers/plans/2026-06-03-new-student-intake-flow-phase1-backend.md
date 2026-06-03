# 新生招生流程整合 Phase 1（後端）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 後端契約先行——讓「有預繳、未註冊」的新生能被暫定編班（綁「年級＋目標學年」保留名額、不建 Student），提供名額規劃彙總，並修正轉換服務使下學年新生 `enrollment_school_year` 正確。

**Architecture:** `recruitment_visits` 新增 `provisional_grade_id / target_school_year / target_semester`；新表 `grade_intake_targets` 作為計畫名額來源。保留座位寫 `recruitment_event_log`。名額彙總集中於純函式服務 `services/recruitment_intake_plan.py`（reserved=未轉換的保留 visit、enrolled=已轉換 Student join visit，兩集合互斥）。轉換服務 `convert_recruitment_to_student` 在 `visit.target_school_year` 有值時改用該學年配學號。3 個 endpoint 掛在新 sub-router `api/recruitment/intake.py`，聚合進既有 `api/recruitment/__init__.py`。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、Pydantic、pytest（in-memory SQLite + StaticPool）。

**Spec:** `docs/superpowers/specs/2026-06-03-new-student-intake-flow-integration-design.md`

**前置（所有 git 指令請用 `git -C /Users/yilunwu/Desktop/ivy-backend`）：** 本 repo 目前可能在 `fix/bug-sweep-2026-06-02-backend` 且工作區有大量未提交 WIP。執行本 plan **前**，請先確認當前分支與工作區狀態，並與 user 確認要把這批 commit 落在哪個分支（建議從 `origin/main` 開 `feat/new-student-intake-be` 乾淨分支；若工作區髒，先 `git -C <repo> status` 列給 user，勿 reset/stash 掉 user WIP）。每個 Task 只 `git add` 該 Task 明確列出的檔案，避免帶進 WIP。

---

## File Structure

| 檔案 | 責任 | 動作 |
|------|------|------|
| `models/recruitment.py` | `RecruitmentVisit` +3 欄；新增 `GradeIntakeTarget` model | Modify |
| `alembic/versions/<ts>_nsintake01_new_student_intake.py` | schema migration | Create |
| `services/recruitment_intake_plan.py` | 保留座位寫入 + 名額彙總純函式 + 計畫名額 upsert + `IntakePlanError` | Create |
| `services/recruitment_conversion.py` | 轉換時依 `visit.target_school_year` 決定學年 | Modify |
| `schemas/recruitment_intake.py` | Pydantic in/out schema | Create |
| `api/recruitment/intake.py` | 3 endpoint 的 sub-router | Create |
| `api/recruitment/__init__.py` | 聚合 intake sub-router | Modify |
| `schemas/recruitment_funnel.py` | `FunnelCard` 增補保留座位欄位 | Modify |
| `api/recruitment/funnel.py` | 抽 `_build_funnel_card` 純函式 + board 帶出保留座位 | Modify |
| `tests/test_recruitment_intake_plan.py` | 名額彙總 + 保留座位純函式測試 | Create |
| `tests/test_recruitment_conversion_target_year.py` | 轉換學年回歸測試 | Create |
| `tests/test_recruitment_intake_api.py` | 3 endpoint route 註冊 smoke | Create |
| `tests/test_recruitment_funnel_card.py` | `_build_funnel_card` 純函式測試 | Create |

---

## Task 1: 資料模型（欄位 + 新表）

**Files:**
- Modify: `models/recruitment.py`（`RecruitmentVisit` class 約 `models/recruitment.py:43-58`，檔尾新增 model）
- Test: `tests/test_recruitment_intake_plan.py`

- [ ] **Step 1: 寫 failing test（模型可建、欄位可寫）**

建立 `tests/test_recruitment_intake_plan.py`：

```python
"""tests/test_recruitment_intake_plan.py — 新生名額規劃模型 + 彙總純函式。"""
import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import ClassGrade, Student, LIFECYCLE_ENROLLED
from models.recruitment import RecruitmentVisit, GradeIntakeTarget


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


def test_model_columns_and_target_table(session):
    grade = ClassGrade(name="中班", sort_order=2)
    session.add(grade)
    session.flush()

    v = RecruitmentVisit(
        month="115.03",
        child_name="王小寶",
        has_deposit=True,
        provisional_grade_id=grade.id,
        target_school_year=115,
        target_semester=1,
    )
    session.add(v)

    t = GradeIntakeTarget(grade_id=grade.id, school_year=115, semester=1, target_seats=30)
    session.add(t)
    session.flush()

    got = session.query(RecruitmentVisit).first()
    assert got.provisional_grade_id == grade.id
    assert got.target_school_year == 115
    assert got.target_semester == 1
    assert session.query(GradeIntakeTarget).first().target_seats == 30
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_recruitment_intake_plan.py::test_model_columns_and_target_table -v`
Expected: FAIL（`ImportError: cannot import name 'GradeIntakeTarget'` 或 `TypeError: 'provisional_grade_id' is an invalid keyword argument`）

- [ ] **Step 3: 在 `RecruitmentVisit` 加三欄**

在 `models/recruitment.py` 的 `RecruitmentVisit`，於 `expected_start_label`（約 line 53-55）之後、`created_at` 之前插入：

```python
    # --- 暫定編班（保留座位；綁年級+目標學年，不綁具體班級 row） ---
    provisional_grade_id = Column(
        Integer, ForeignKey("class_grades.id", ondelete="SET NULL"), nullable=True
    )  # 暫定年級
    target_school_year = Column(Integer, nullable=True)  # 目標學年（民國，如 115）
    target_semester = Column(Integer, nullable=True, default=1)  # 目標學期（1=上）
```

並在 `__table_args__`（約 line 60-69）的 tuple 內新增一條索引：

```python
        Index(
            "ix_rv_target_grade",
            "target_school_year",
            "target_semester",
            "provisional_grade_id",
        ),
```

- [ ] **Step 4: 在檔尾新增 `GradeIntakeTarget` model**

在 `models/recruitment.py` 檔案最末加入：

```python
class GradeIntakeTarget(Base):
    """各年級各學年的招生「計畫名額」（名額規劃面板的 target 來源）。"""

    __tablename__ = "grade_intake_targets"

    id = Column(Integer, primary_key=True, index=True)
    grade_id = Column(
        Integer, ForeignKey("class_grades.id", ondelete="CASCADE"), nullable=False
    )
    school_year = Column(Integer, nullable=False)  # 民國學年
    semester = Column(Integer, nullable=False, default=1)
    target_seats = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)

    __table_args__ = (
        Index(
            "uq_grade_intake_target",
            "grade_id",
            "school_year",
            "semester",
            unique=True,
        ),
    )
```

（`Column / Integer / ForeignKey / DateTime / Index / now_taipei_naive` 皆已在檔頭 import，無需新增 import。）

- [ ] **Step 5: 跑測試確認通過**

Run: `python -m pytest tests/test_recruitment_intake_plan.py::test_model_columns_and_target_table -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add models/recruitment.py tests/test_recruitment_intake_plan.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(recruitment): 新生暫定編班欄位 + 計畫名額表 model

recruitment_visits 加 provisional_grade_id/target_school_year/target_semester；
新增 grade_intake_targets 表（計畫名額來源）。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Alembic migration `nsintake01`

**Files:**
- Create: `alembic/versions/20260603_nsintake01_new_student_intake.py`

- [ ] **Step 1: 取得目前 head（migration 的 down_revision 來源）**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -m alembic heads`
Expected: 印出一個 head revision id（記下它，例如 `abc123def456`）。
若印出 **多個** head → 先停下回報 user（需先 merge heads，勿自行猜測；見 memory「Alembic 多 head」教訓）。

- [ ] **Step 2: 建立 migration 檔**

Create `alembic/versions/20260603_nsintake01_new_student_intake.py`：

```python
"""new student intake: provisional seat fields + grade_intake_targets

Revision ID: nsintake01
Revises: <HEAD>
Create Date: 2026-06-03
"""
from alembic import op
import sqlalchemy as sa

revision = "nsintake01"
down_revision = "<HEAD>"  # ← 填入 Step 1 `alembic heads` 的輸出
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "recruitment_visits",
        sa.Column("provisional_grade_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "recruitment_visits",
        sa.Column("target_school_year", sa.Integer(), nullable=True),
    )
    op.add_column(
        "recruitment_visits",
        sa.Column("target_semester", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_rv_provisional_grade",
        "recruitment_visits",
        "class_grades",
        ["provisional_grade_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_rv_target_grade",
        "recruitment_visits",
        ["target_school_year", "target_semester", "provisional_grade_id"],
    )

    op.create_table(
        "grade_intake_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "grade_id",
            sa.Integer(),
            sa.ForeignKey("class_grades.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("school_year", sa.Integer(), nullable=False),
        sa.Column("semester", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("target_seats", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "uq_grade_intake_target",
        "grade_intake_targets",
        ["grade_id", "school_year", "semester"],
        unique=True,
    )


def downgrade():
    op.drop_index("uq_grade_intake_target", table_name="grade_intake_targets")
    op.drop_table("grade_intake_targets")
    op.drop_index("ix_rv_target_grade", table_name="recruitment_visits")
    op.drop_constraint("fk_rv_provisional_grade", "recruitment_visits", type_="foreignkey")
    op.drop_column("recruitment_visits", "target_semester")
    op.drop_column("recruitment_visits", "target_school_year")
    op.drop_column("recruitment_visits", "provisional_grade_id")
```

- [ ] **Step 3: 套用並驗證可逆**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend
python -m alembic upgrade heads
python -m alembic downgrade -1
python -m alembic upgrade heads
```
Expected: 三段皆無錯誤；`upgrade` 後 `recruitment_visits` 有新欄、`grade_intake_targets` 存在。
（注意：本 repo 的 baseline migration 從**空** DB 直接 upgrade 會炸是已知現象；此處針對既有 dev DB 增量套用，不受影響。）

- [ ] **Step 4: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add alembic/versions/20260603_nsintake01_new_student_intake.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(recruitment): migration nsintake01 新生暫定編班 schema

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: 名額彙總純函式

**Files:**
- Create: `services/recruitment_intake_plan.py`
- Test: `tests/test_recruitment_intake_plan.py`（沿用 Task 1 的 fixture）

- [ ] **Step 1: 寫 failing test（彙總 + 不重複計數）**

在 `tests/test_recruitment_intake_plan.py` 末尾追加：

```python
from services.recruitment_intake_plan import compute_intake_plan


def _grade(session, name, order):
    g = ClassGrade(name=name, sort_order=order)
    session.add(g)
    session.flush()
    return g


def test_compute_intake_plan_counts(session):
    mid = _grade(session, "中班", 2)
    big = _grade(session, "大班", 3)
    session.add(GradeIntakeTarget(grade_id=mid.id, school_year=115, semester=1, target_seats=30))
    session.flush()

    # 2 筆已保留（未轉換）中班
    for nm in ("甲", "乙"):
        session.add(RecruitmentVisit(
            month="115.03", child_name=nm, has_deposit=True,
            provisional_grade_id=mid.id, target_school_year=115, target_semester=1,
            enrolled=False,
        ))
    # 1 筆已轉換中班 visit + 對應 Student（不應再算進「保留」，要算「註冊」）
    conv = RecruitmentVisit(
        month="115.03", child_name="丙", has_deposit=True,
        provisional_grade_id=mid.id, target_school_year=115, target_semester=1,
        enrolled=True,
    )
    session.add(conv)
    session.flush()
    # 顯式給 student_id（nullable=False；正常由 before_flush listener 自動填，
    # 此處顯式設定以免測試依賴 listener 內部行為）
    session.add(Student(
        student_id="115T001", name="丙", enrollment_school_year=115, enrollment_seq=1,
        lifecycle_status=LIFECYCLE_ENROLLED, recruitment_visit_id=conv.id,
    ))
    session.flush()

    rows = compute_intake_plan(session, school_year=115, semester=1)
    by_grade = {r["grade_id"]: r for r in rows}

    assert by_grade[mid.id]["reserved_count"] == 2
    assert by_grade[mid.id]["enrolled_count"] == 1
    assert by_grade[mid.id]["target_seats"] == 30
    assert by_grade[mid.id]["remaining"] == 27  # 30 - 2 - 1
    assert by_grade[mid.id]["over_capacity"] is False
    # 大班無 target、無保留/註冊 → target 0、remaining 0
    assert by_grade[big.id]["target_seats"] == 0
    assert by_grade[big.id]["remaining"] == 0


def test_over_capacity_flag(session):
    mid = _grade(session, "中班", 2)
    session.add(GradeIntakeTarget(grade_id=mid.id, school_year=115, semester=1, target_seats=1))
    for nm in ("甲", "乙"):
        session.add(RecruitmentVisit(
            month="115.03", child_name=nm, has_deposit=True,
            provisional_grade_id=mid.id, target_school_year=115, target_semester=1,
            enrolled=False,
        ))
    session.flush()
    rows = {r["grade_id"]: r for r in compute_intake_plan(session, school_year=115, semester=1)}
    assert rows[mid.id]["over_capacity"] is True
    assert rows[mid.id]["remaining"] == -1
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_recruitment_intake_plan.py -k "intake_plan_counts or over_capacity" -v`
Expected: FAIL（`ModuleNotFoundError: services.recruitment_intake_plan`）

- [ ] **Step 3: 建立 service 檔 + `compute_intake_plan`**

Create `services/recruitment_intake_plan.py`：

```python
"""services/recruitment_intake_plan.py — 新生名額規劃。

職責：
- compute_intake_plan(): 以「目標學年 × 學期」彙總每年級 計畫/保留/註冊/剩餘。
- set_provisional_seat(): 設定/釋放某訪視的暫定編班（保留座位）。
- upsert_intake_targets(): 設定計畫名額。

名額計算單一真相（spec §7）：
- reserved = recruitment_visits 有 provisional_grade_id 且 enrolled=False。
- enrolled = Student join 其 recruitment_visit（visit.provisional_grade_id 為年級歸屬），
  Student.enrollment_school_year=目標學年 且 lifecycle 非終態。
兩集合以 enrolled 旗標互斥，無重複計數。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.classroom import (
    ClassGrade,
    Student,
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
)
from models.recruitment import GradeIntakeTarget, RecruitmentVisit

_TERMINAL = (LIFECYCLE_GRADUATED, LIFECYCLE_TRANSFERRED, LIFECYCLE_WITHDRAWN)


class IntakePlanError(ValueError):
    """名額規劃業務錯誤。"""


def compute_intake_plan(session: Session, *, school_year: int, semester: int = 1) -> list[dict]:
    """回傳每個 active 年級一列：target / reserved / enrolled / remaining / over_capacity。"""
    # reserved：未轉換的保留 visit，依 provisional_grade_id 分組
    reserved_rows = (
        session.query(RecruitmentVisit.provisional_grade_id, func.count(RecruitmentVisit.id))
        .filter(
            RecruitmentVisit.provisional_grade_id.isnot(None),
            RecruitmentVisit.target_school_year == school_year,
            RecruitmentVisit.target_semester == semester,
            RecruitmentVisit.enrolled.is_(False),
        )
        .group_by(RecruitmentVisit.provisional_grade_id)
        .all()
    )
    reserved_by_grade = {gid: cnt for gid, cnt in reserved_rows}

    # enrolled：Student join visit；以 visit.provisional_grade_id 為年級歸屬
    enrolled_rows = (
        session.query(RecruitmentVisit.provisional_grade_id, func.count(Student.id))
        .join(Student, Student.recruitment_visit_id == RecruitmentVisit.id)
        .filter(
            RecruitmentVisit.provisional_grade_id.isnot(None),
            RecruitmentVisit.target_semester == semester,
            Student.enrollment_school_year == school_year,
            Student.lifecycle_status.notin_(_TERMINAL),
        )
        .group_by(RecruitmentVisit.provisional_grade_id)
        .all()
    )
    enrolled_by_grade = {gid: cnt for gid, cnt in enrolled_rows}

    target_rows = (
        session.query(GradeIntakeTarget.grade_id, GradeIntakeTarget.target_seats)
        .filter(
            GradeIntakeTarget.school_year == school_year,
            GradeIntakeTarget.semester == semester,
        )
        .all()
    )
    target_by_grade = {gid: seats for gid, seats in target_rows}

    grades = (
        session.query(ClassGrade)
        .filter(ClassGrade.is_active.is_(True))
        .order_by(ClassGrade.sort_order, ClassGrade.id)
        .all()
    )

    rows: list[dict] = []
    for g in grades:
        reserved = int(reserved_by_grade.get(g.id, 0))
        enrolled = int(enrolled_by_grade.get(g.id, 0))
        target = int(target_by_grade.get(g.id, 0))
        rows.append(
            {
                "grade_id": g.id,
                "grade_name": g.name,
                "target_seats": target,
                "reserved_count": reserved,
                "enrolled_count": enrolled,
                "remaining": target - reserved - enrolled,
                "over_capacity": (reserved + enrolled) > target,
            }
        )
    return rows
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_recruitment_intake_plan.py -k "intake_plan_counts or over_capacity" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add services/recruitment_intake_plan.py tests/test_recruitment_intake_plan.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(recruitment): 名額規劃彙總純函式 compute_intake_plan

reserved/enrolled 以 enrolled 旗標互斥，無重複計數。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 暫定編班（設定/釋放）+ 計畫名額 upsert

**Files:**
- Modify: `services/recruitment_intake_plan.py`
- Test: `tests/test_recruitment_intake_plan.py`

- [ ] **Step 1: 寫 failing test（守衛 + 設定/釋放 + event log + upsert）**

在 `tests/test_recruitment_intake_plan.py` 末尾追加：

```python
from models.recruitment import RecruitmentEventLog
from services.recruitment_intake_plan import (
    IntakePlanError, set_provisional_seat, upsert_intake_targets,
)


def test_set_provisional_seat_requires_deposit(session):
    mid = _grade(session, "中班", 2)
    v = RecruitmentVisit(month="115.03", child_name="甲", has_deposit=False)
    session.add(v)
    session.flush()
    with pytest.raises(IntakePlanError):
        set_provisional_seat(
            session, visit_id=v.id, provisional_grade_id=mid.id,
            target_school_year=115, target_semester=1, actor_user_id=None,
        )


def test_set_then_release_provisional_seat(session):
    mid = _grade(session, "中班", 2)
    v = RecruitmentVisit(month="115.03", child_name="甲", has_deposit=True)
    session.add(v)
    session.flush()

    set_provisional_seat(
        session, visit_id=v.id, provisional_grade_id=mid.id,
        target_school_year=115, target_semester=1, actor_user_id=7,
    )
    session.flush()
    got = session.query(RecruitmentVisit).get(v.id)
    assert got.provisional_grade_id == mid.id
    assert got.target_school_year == 115
    assert session.query(RecruitmentEventLog).filter_by(event_type="seat_reserved").count() == 1

    # 釋放
    set_provisional_seat(
        session, visit_id=v.id, provisional_grade_id=None,
        target_school_year=None, target_semester=None, actor_user_id=7,
    )
    session.flush()
    got = session.query(RecruitmentVisit).get(v.id)
    assert got.provisional_grade_id is None
    assert session.query(RecruitmentEventLog).filter_by(event_type="seat_released").count() == 1


def test_upsert_intake_targets(session):
    mid = _grade(session, "中班", 2)
    upsert_intake_targets(
        session, school_year=115, semester=1,
        targets=[{"grade_id": mid.id, "target_seats": 25}],
    )
    session.flush()
    assert session.query(GradeIntakeTarget).filter_by(grade_id=mid.id).one().target_seats == 25
    # 再 upsert 同鍵 → 更新而非新增
    upsert_intake_targets(
        session, school_year=115, semester=1,
        targets=[{"grade_id": mid.id, "target_seats": 40}],
    )
    session.flush()
    assert session.query(GradeIntakeTarget).filter_by(grade_id=mid.id).count() == 1
    assert session.query(GradeIntakeTarget).filter_by(grade_id=mid.id).one().target_seats == 40
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_recruitment_intake_plan.py -k "provisional_seat or intake_targets" -v`
Expected: FAIL（`ImportError: cannot import name 'set_provisional_seat'`）

- [ ] **Step 3: 在 service 加 `set_provisional_seat` 與 `upsert_intake_targets`**

在 `services/recruitment_intake_plan.py` 末尾加入（檔頭 import 補 `from utils.taipei_time import now_taipei_naive` 與 `from models.recruitment import RecruitmentEventLog`）：

```python
def set_provisional_seat(
    session: Session,
    *,
    visit_id: int,
    provisional_grade_id: Optional[int],
    target_school_year: Optional[int],
    target_semester: Optional[int],
    actor_user_id: Optional[int],
) -> RecruitmentVisit:
    """設定（provisional_grade_id 非 None）或釋放（None）某訪視的保留座位。

    守衛：設定時 visit.has_deposit 必須為 True。寫 recruitment_event_log。
    Commit 由 caller 負責。
    """
    visit = session.query(RecruitmentVisit).filter_by(id=visit_id).first()
    if visit is None:
        raise IntakePlanError(f"招生訪視不存在：id={visit_id}")

    is_set = provisional_grade_id is not None
    if is_set and not visit.has_deposit:
        raise IntakePlanError("未預繳的訪視不可保留座位")
    if is_set and target_school_year is None:
        raise IntakePlanError("保留座位需指定目標學年")

    visit.provisional_grade_id = provisional_grade_id
    visit.target_school_year = target_school_year
    visit.target_semester = target_semester if is_set else None

    log = RecruitmentEventLog(
        recruitment_visit_id=visit.id,
        event_type="seat_reserved" if is_set else "seat_released",
        from_stage="deposited",
        to_stage="deposited",
        actor_user_id=actor_user_id,
        metadata_json={
            "grade_id": provisional_grade_id,
            "school_year": target_school_year,
            "semester": target_semester if is_set else None,
        },
        created_at=now_taipei_naive(),
    )
    session.add(log)
    session.flush()
    return visit


def upsert_intake_targets(
    session: Session,
    *,
    school_year: int,
    semester: int,
    targets: list[dict],
) -> list[GradeIntakeTarget]:
    """以 (grade_id, school_year, semester) upsert 計畫名額。Commit 由 caller 負責。"""
    result: list[GradeIntakeTarget] = []
    for item in targets:
        gid = int(item["grade_id"])
        seats = int(item["target_seats"])
        row = (
            session.query(GradeIntakeTarget)
            .filter_by(grade_id=gid, school_year=school_year, semester=semester)
            .first()
        )
        if row is None:
            row = GradeIntakeTarget(
                grade_id=gid, school_year=school_year, semester=semester, target_seats=seats
            )
            session.add(row)
        else:
            row.target_seats = seats
        result.append(row)
    session.flush()
    return result
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_recruitment_intake_plan.py -v`
Expected: PASS（本檔全綠）

- [ ] **Step 5: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add services/recruitment_intake_plan.py tests/test_recruitment_intake_plan.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(recruitment): 保留座位設定/釋放 + 計畫名額 upsert

set_provisional_seat 守衛 has_deposit、寫 seat_reserved/seat_released event。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: 轉換服務依目標學年配學號

**Files:**
- Modify: `services/recruitment_conversion.py:87-88` 與 `:134`
- Test: `tests/test_recruitment_conversion_target_year.py`

- [ ] **Step 1: 寫 failing test（目標學年 + 回歸）**

Create `tests/test_recruitment_conversion_target_year.py`：

```python
"""下學年新生轉換：enrollment_school_year 取自 visit.target_school_year。"""
import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Student
from models.recruitment import RecruitmentVisit
from services.recruitment_conversion import convert_recruitment_to_student


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


def test_uses_target_school_year_when_set(session):
    v = RecruitmentVisit(
        month="115.03", child_name="甲", has_deposit=True,
        target_school_year=999, target_semester=1,  # 用不可能的學年凸顯來源
    )
    session.add(v)
    session.commit()
    result = convert_recruitment_to_student(session, recruitment_visit_id=v.id)
    session.commit()
    student = session.query(Student).get(result.student_id)
    assert student.enrollment_school_year == 999


def test_falls_back_to_current_term_when_no_target(session):
    from utils.academic import resolve_current_academic_term
    cur_year, _ = resolve_current_academic_term()
    v = RecruitmentVisit(month="115.03", child_name="乙", has_deposit=True)
    session.add(v)
    session.commit()
    result = convert_recruitment_to_student(session, recruitment_visit_id=v.id)
    session.commit()
    student = session.query(Student).get(result.student_id)
    assert student.enrollment_school_year == cur_year
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_recruitment_conversion_target_year.py -v`
Expected: `test_uses_target_school_year_when_set` FAIL（取到當前學年而非 999）；另一個應 PASS。

- [ ] **Step 3: 改 `convert_recruitment_to_student`**

在 `services/recruitment_conversion.py`，將 line 87 附近：

```python
    enroll_year, _ = resolve_current_academic_term()
    seq = next_enrollment_seq(session, enroll_year)
```

改為：

```python
    cur_year, cur_sem = resolve_current_academic_term()
    enroll_year = visit.target_school_year if visit.target_school_year is not None else cur_year
    enroll_sem = visit.target_semester if visit.target_semester is not None else cur_sem
    seq = next_enrollment_seq(session, enroll_year)
```

並將 line 134 的：

```python
    school_year, semester = resolve_current_academic_term()
```

改為（沿用上面已算出的學年/學期，讓 ChangeLog 與學號同基準）：

```python
    school_year, semester = enroll_year, enroll_sem
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_recruitment_conversion_target_year.py tests/test_recruitment_conversion.py -v`
Expected: 全 PASS（新測試 + 既有轉換測試回歸綠）

- [ ] **Step 5: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add services/recruitment_conversion.py tests/test_recruitment_conversion_target_year.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "fix(recruitment): 下學年新生轉換用 target_school_year 配學號

避免永久學號 (enrollment_school_year, seq) 配到錯誤學年。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Pydantic schema

**Files:**
- Create: `schemas/recruitment_intake.py`

- [ ] **Step 1: 建立 schema 檔**

Create `schemas/recruitment_intake.py`：

```python
"""schemas/recruitment_intake.py — 新生名額規劃 in/out。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ReserveSeatIn(BaseModel):
    provisional_grade_id: Optional[int] = Field(
        None, description="暫定年級；null = 釋放保留座位"
    )
    target_school_year: Optional[int] = Field(None, description="目標學年（民國）")
    target_semester: Optional[int] = Field(None, description="目標學期，省略預設 1")


class ReserveSeatOut(BaseModel):
    visit_id: int
    provisional_grade_id: Optional[int]
    provisional_grade_name: Optional[str]
    target_school_year: Optional[int]
    target_semester: Optional[int]


class IntakePlanRow(BaseModel):
    grade_id: int
    grade_name: str
    target_seats: int
    reserved_count: int
    enrolled_count: int
    remaining: int
    over_capacity: bool


class IntakePlanOut(BaseModel):
    school_year: int
    semester: int
    rows: list[IntakePlanRow]


class IntakeTargetItem(BaseModel):
    grade_id: int
    target_seats: int = Field(ge=0)


class IntakeTargetsIn(BaseModel):
    school_year: int
    semester: int = 1
    targets: list[IntakeTargetItem]


class IntakeTargetsOut(BaseModel):
    school_year: int
    semester: int
    targets: list[IntakeTargetItem]
```

- [ ] **Step 2: 確認可 import（無語法錯）**

Run: `python -c "import schemas.recruitment_intake as m; print(m.IntakePlanOut.__name__)"`
Expected: 印出 `IntakePlanOut`

- [ ] **Step 3: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add schemas/recruitment_intake.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(recruitment): 新生名額規劃 Pydantic schema

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: API 端點 + router 聚合

**Files:**
- Create: `api/recruitment/intake.py`
- Modify: `api/recruitment/__init__.py:97-104`（聚合）
- Test: `tests/test_recruitment_intake_api.py`

- [ ] **Step 1: 寫 failing test（route 註冊 smoke）**

> 設計理由：既有 `tests/test_recruitment_funnel_api_smoke.py` 對招生 API 只做「router 匯入 + 路徑註冊 + 聚合進 main.app」的 smoke；**行為**（守衛 / 彙總 / upsert）已在 service 層用真 in-memory session 測過（Task 3–4）。且 `require_staff_permission(perm)` 每次呼叫回傳**新** callable，`dependency_overrides` 對不上、無法簡單繞過；要打真 HTTP 需照 `test_fee_templates.py` swap `models.base._engine/_SessionFactory` + 建真 admin 登入，成本高且非本 repo 對此模組的慣例。故 API 層維持 smoke。

Create `tests/test_recruitment_intake_api.py`：

```python
"""新生名額規劃 API smoke：router 匯入、路徑註冊、聚合進 main.app。
行為（守衛/彙總/upsert）由 service 層測試覆蓋（test_recruitment_intake_plan.py）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_intake_router_imports_and_paths():
    from api.recruitment.intake import router

    paths = {r.path for r in router.routes}
    assert any(p.endswith("/reserve-seat") for p in paths)
    assert any(p.endswith("/intake-plan") for p in paths)
    assert any(p.endswith("/intake-targets") for p in paths)


def test_main_app_includes_intake_routes():
    import main

    app_paths = {r.path for r in main.app.routes}
    assert any("reserve-seat" in p for p in app_paths)
    assert any("intake-plan" in p for p in app_paths)
    assert any("intake-targets" in p for p in app_paths)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_recruitment_intake_api.py -v`
Expected: FAIL（`ModuleNotFoundError: api.recruitment.intake`）

- [ ] **Step 3: 建立 `api/recruitment/intake.py`**

Create `api/recruitment/intake.py`：

```python
"""新生名額規劃 API：保留座位、名額彙總、計畫名額。"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from models.base import session_scope
from models.classroom import ClassGrade
from models.recruitment import RecruitmentVisit
from services.recruitment_intake_plan import (
    IntakePlanError,
    compute_intake_plan,
    set_provisional_seat,
    upsert_intake_targets,
)
from utils.auth import require_staff_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from schemas.recruitment_intake import (
    IntakePlanOut,
    IntakeTargetsIn,
    IntakeTargetsOut,
    ReserveSeatIn,
    ReserveSeatOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recruitment", tags=["recruitment-intake"])


@router.post("/funnel/visits/{visit_id}/reserve-seat", response_model=ReserveSeatOut)
def reserve_seat(
    visit_id: int,
    payload: ReserveSeatIn,
    current_user: dict = Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    """設定/釋放暫定編班（保留座位）。null grade = 釋放。"""
    try:
        with session_scope() as session:
            try:
                visit = set_provisional_seat(
                    session,
                    visit_id=visit_id,
                    provisional_grade_id=payload.provisional_grade_id,
                    target_school_year=payload.target_school_year,
                    target_semester=(
                        payload.target_semester
                        if payload.target_semester is not None
                        else (1 if payload.provisional_grade_id is not None else None)
                    ),
                    actor_user_id=current_user.get("user_id"),
                )
                grade_name = None
                if visit.provisional_grade_id is not None:
                    g = session.query(ClassGrade).get(visit.provisional_grade_id)
                    grade_name = g.name if g else None
                out = ReserveSeatOut(
                    visit_id=visit.id,
                    provisional_grade_id=visit.provisional_grade_id,
                    provisional_grade_name=grade_name,
                    target_school_year=visit.target_school_year,
                    target_semester=visit.target_semester,
                )
            except IntakePlanError as e:
                raise HTTPException(status_code=400, detail=str(e))
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="保留座位失敗")


@router.get("/intake-plan", response_model=IntakePlanOut)
def get_intake_plan(
    school_year: int = Query(...),
    semester: int = Query(1),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """名額規劃彙總：每年級 計畫/保留/註冊/剩餘。"""
    try:
        with session_scope() as session:
            rows = compute_intake_plan(session, school_year=school_year, semester=semester)
        return IntakePlanOut(school_year=school_year, semester=semester, rows=rows)
    except Exception as e:
        raise_safe_500(e, context="名額彙總失敗")


@router.put("/intake-targets", response_model=IntakeTargetsOut)
def put_intake_targets(
    payload: IntakeTargetsIn,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    """設定每年級計畫名額。"""
    try:
        with session_scope() as session:
            rows = upsert_intake_targets(
                session,
                school_year=payload.school_year,
                semester=payload.semester,
                targets=[t.model_dump() for t in payload.targets],
            )
            out = IntakeTargetsOut(
                school_year=payload.school_year,
                semester=payload.semester,
                targets=[
                    {"grade_id": r.grade_id, "target_seats": r.target_seats} for r in rows
                ],
            )
        return out
    except Exception as e:
        raise_safe_500(e, context="設定計畫名額失敗")
```

- [ ] **Step 4: 聚合 router**

在 `api/recruitment/__init__.py`：於 `from api.recruitment import (...)`（約 line 23-32）的 sub-module import 區，加入 `intake as _intake`；並在 router 聚合區（約 line 97-104）`router.include_router(_funnel.router)` 之後新增：

```python
router.include_router(_intake.router)
```

- [ ] **Step 5: 跑測試確認通過**

Run: `python -m pytest tests/test_recruitment_intake_api.py -v`
Expected: PASS（router 匯入成功、3 路徑註冊、聚合進 main.app）

- [ ] **Step 6: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add api/recruitment/intake.py api/recruitment/__init__.py tests/test_recruitment_intake_api.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(recruitment): 名額規劃 3 端點（保留座位/彙總/計畫名額）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: 漏斗看板卡片帶出保留座位欄位

**Files:**
- Modify: `schemas/recruitment_funnel.py`（`FunnelCard`）
- Modify: `api/recruitment/funnel.py`（抽 `_build_funnel_card` 純函式 + `get_board` 改用）
- Test: `tests/test_recruitment_funnel_card.py`

- [ ] **Step 1: 找到 `FunnelCard` 定義**

Run: `grep -n "class FunnelCard" schemas/recruitment_funnel.py`
Expected: 印出行號。確認現有欄位（`visit_id / child_name / grade / phone / district / source / deposited_at / student_id / current_stage`）。

- [ ] **Step 2: 寫 failing test（純函式直接驗，免 DB/auth）**

設計理由：board endpoint 需 `Depends` session/權限，HTTP 測試成本高（見 Task 7 Step 1）。改把卡片組裝抽成純函式 `_build_funnel_card(visit, student, grade_name_map)`，可用記憶體中（未存檔）的 `RecruitmentVisit` 物件直接測。

Create `tests/test_recruitment_funnel_card.py`：

```python
"""_build_funnel_card 純函式：保留座位欄位正確帶出。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.recruitment import RecruitmentVisit
from api.recruitment.funnel import _build_funnel_card


def test_card_exposes_reserved_seat():
    v = RecruitmentVisit(
        month="115.03", child_name="甲", has_deposit=True,
        provisional_grade_id=3, target_school_year=115, target_semester=1,
    )
    v.id = 1  # 未存檔，手動指定 id 供卡片使用
    card = _build_funnel_card(v, None, {3: "中班"})
    assert card.current_stage == "deposited"
    assert card.provisional_grade_id == 3
    assert card.provisional_grade_name == "中班"
    assert card.target_school_year == 115


def test_card_without_reservation():
    v = RecruitmentVisit(month="115.03", child_name="乙", has_deposit=False)
    v.id = 2
    card = _build_funnel_card(v, None, {})
    assert card.provisional_grade_id is None
    assert card.provisional_grade_name is None
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `python -m pytest tests/test_recruitment_funnel_card.py -v`
Expected: FAIL（`ImportError: cannot import name '_build_funnel_card'`）

- [ ] **Step 4: `FunnelCard` 加三個 optional 欄位**

在 `schemas/recruitment_funnel.py` 的 `FunnelCard`，於 `current_stage` 之後新增：

```python
    provisional_grade_id: Optional[int] = None
    provisional_grade_name: Optional[str] = None
    target_school_year: Optional[int] = None
```

（確認檔頭已 `from typing import Optional`；若無則補。）

- [ ] **Step 5: 抽出 `_build_funnel_card` 並讓 `get_board` 改用**

在 `api/recruitment/funnel.py`，於 `get_board` 之前新增模組層純函式（`derive_stage`、`FunnelCard` 皆已在本檔 import）：

```python
def _build_funnel_card(visit, student, grade_name_map):
    """把一筆訪視（+ 對應 student）組成看板卡片；純函式、不碰 session。"""
    stage = derive_stage(visit, student)
    return FunnelCard(
        visit_id=visit.id,
        child_name=visit.child_name,
        grade=visit.grade,
        phone=visit.phone,
        district=visit.district,
        source=visit.source,
        deposited_at=visit.updated_at if visit.has_deposit else None,
        student_id=student.id if student else None,
        current_stage=stage,
        provisional_grade_id=visit.provisional_grade_id,
        provisional_grade_name=grade_name_map.get(visit.provisional_grade_id),
        target_school_year=visit.target_school_year,
    )
```

把 `get_board` 內既有的 board 迴圈（約 `api/recruitment/funnel.py:67-89`）改為：

```python
    from models.classroom import ClassGrade
    grade_name_map: dict[int, str] = {
        g.id: g.name for g in session.query(ClassGrade).all()
    }

    buckets: dict[str, list[FunnelCard]] = {
        "visited": [],
        "deposited": [],
        "enrolled": [],
        "active": [],
    }
    for v in visits:
        student = student_map.get(v.id)
        card = _build_funnel_card(v, student, grade_name_map)
        buckets[card.current_stage].append(card)
```

（`student_map` 與 `visits` 的既有查詢不變；`FunnelSummary` 仍以 `len(buckets[...])` 計。）

- [ ] **Step 6: 跑測試確認通過**

Run: `python -m pytest tests/test_recruitment_funnel_card.py tests/test_recruitment_funnel_api_smoke.py -v`
Expected: PASS（新純函式測試 + 既有 funnel smoke 皆綠）

- [ ] **Step 7: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add schemas/recruitment_funnel.py api/recruitment/funnel.py tests/test_recruitment_funnel_card.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(recruitment): 漏斗看板卡片帶出保留座位（年級/目標學年）

抽 _build_funnel_card 純函式便於單元測試。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: 全套件回歸 + OpenAPI dump（前端 codegen 準備）

**Files:** 無新增（驗證 + 產 artifact）

- [ ] **Step 1: 跑招生相關測試全綠**

Run: `python -m pytest tests/ -k "recruitment or intake or funnel or conversion" -q`
Expected: 全 PASS（無新增 fail）。若環境會 hang，至少跑本 plan 新增的 4 個測試檔 + `test_recruitment_conversion.py` + `test_recruitment_funnel_transitions.py`。

- [ ] **Step 2: 產 OpenAPI（給前端 Phase 2 codegen）**

Run: `python scripts/dump_openapi.py`
Expected: 產出 `openapi.json`（local-only，`.gitignore` 擋，不 commit）。確認其中含 `/recruitment/intake-plan`、`/recruitment/intake-targets`、`/recruitment/funnel/visits/{visit_id}/reserve-seat`。

- [ ] **Step 3: （可選）更新 workspace CLAUDE.md 對應表**

若要，於 `~/Desktop/ivyManageSystem/CLAUDE.md` 的「API 模組 ↔ 後端 Router 對應慣例」表補 `recruitmentIntakePlan.ts ↔ api/recruitment/intake.py`（前端檔 Phase 2 才建，可延後）。本步驟非阻塞。

- [ ] **Step 4: 無程式碼變更則不需 commit**；若有更新文件則單獨 commit。

---

## 完成定義（Phase 1）
- migration `nsintake01` 可 upgrade/downgrade。
- 3 endpoint 可用、`reserve-seat` 守衛 `has_deposit` 生效。
- `compute_intake_plan` reserved/enrolled 不重複計數、`over_capacity` 正確。
- 下學年新生轉換 `enrollment_school_year` = 目標學年；既有轉換測試回歸綠。
- 漏斗看板卡片帶出保留座位欄位。
- 不新增 Permission、不動既有 4 階段狀態機。

## 後續（不在本 plan）
- **Phase 2 前端**：`reserveSeat` / `getIntakePlan` / `setIntakeTargets` api 層 + 漏斗卡片徽章/編班對話框 + 「新生名額規劃」面板 + `npm run gen:api`。後端合併並 `alembic upgrade heads` 後另立 plan。
- 官網報名整合（spec §12 未來工作）。
