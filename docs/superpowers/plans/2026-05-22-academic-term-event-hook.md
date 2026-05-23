# 學期切換 hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 補上學期切換的 source of truth（`AcademicTerm.is_current` flag）、同步 hooks registry、三個 subscriber（classroom carry-over / leave_quota cutover / activity semester tag placeholder）、以及過渡期 `_check_quota` 學年優先讀路徑。

**Architecture:** Admin POST `/academic-terms/{id}/set-current` → 同 transaction 內 UPDATE `is_current` flag → `fire_term_changed()` 依序串註呼叫 subscriber → 任一 raise 整 transaction rollback。Helper `resolve_current_academic_term()` 切成「優先讀 DB、找不到 fallback 日期推算 + warning log」。

**Tech Stack:** FastAPI / SQLAlchemy / Alembic / PostgreSQL（dev + prod）/ SQLite（test）/ pytest。

**Worktree:** `feat/recruitment-funnel-phase-a-2026-05-22-backend` HEAD `580658b`（spec 已 commit）。

**Spec reference:** `docs/superpowers/specs/2026-05-22-academic-term-event-hook-design.md`

---

## File Structure

**Files to create:**
- `alembic/versions/20260522_acadhk01_academic_term_is_current_and_leave_quota_school_year.py` — Alembic migration（schema 增量）
- `utils/term_events.py` — hooks registry（`@on_term_changed` decorator + `fire_term_changed()`）
- `services/term_subscribers/__init__.py` — 空檔（package marker）
- `services/term_subscribers/classroom_carry_over.py` — Subscriber 1
- `services/term_subscribers/leave_quota_cutover.py` — Subscriber 2
- `services/term_subscribers/activity_semester_tag.py` — Subscriber 3 placeholder
- `tests/test_term_events.py` — hooks registry unit tests
- `tests/test_classroom_carry_over.py` — subscriber 1 unit tests
- `tests/test_leave_quota_cutover.py` — subscriber 2 unit tests
- `tests/test_term_change_integration.py` — toggle endpoint 端對端

**Files to modify:**
- `models/academic_term.py` — 加 `is_current` 欄位 + partial unique index
- `models/leave.py` — 加 `school_year` 欄位 + partial unique index + index
- `models/classroom.py` — `_default_school_year` / `_default_semester` 改 import `default_current_academic_term_for_column`
- `utils/academic.py` — 改寫 helper（拆 `_resolve_by_date` / `default_current_academic_term_for_column`、`resolve_current_academic_term` 接 session）
- `api/academic_terms.py` — 加 `POST /{term_id}/set-current` endpoint
- `api/leaves_quota.py` — `_calc_annual_leave_hours` 擴 `reference_date` 參數、`_resolve_quota_row` 新函式、`_check_quota` / `_check_compensatory_quota` 改用 `_resolve_quota_row`
- `main.py` — 在 `on_startup()` 加三個 subscriber import + handlers sanity log
- `tests/test_academic_utils.py` — 擴充 DB-aware 新測試

**Test layout** mirror source（既有慣例）：`tests/test_<module>.py` 對 `<module>.py`。

---

## 預備：worktree sanity check

- [ ] **Verify cwd 與 branch**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/recruitment-funnel-phase-a-be
git branch --show-current
git log --oneline -2
```
Expected:
```
feat/recruitment-funnel-phase-a-2026-05-22-backend
580658b docs(spec): 學期切換 hook 後端設計 2026-05-22
9a30bf5 chore(api): mark old /convert endpoint deprecated
```

- [ ] **Verify pytest baseline 綠**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/recruitment-funnel-phase-a-be
pytest tests/test_academic_utils.py tests/test_activity_academic_term.py -x -q
```
Expected: PASS（既有 19 個 test 全綠）

---

## Task 1: Alembic migration — `acadhk01`

**Files:**
- Create: `alembic/versions/20260522_acadhk01_academic_term_is_current_and_leave_quota_school_year.py`

**Goal:** AcademicTerm 加 `is_current` 欄位 + partial unique singleton index；LeaveQuota 加 `school_year` 欄位 + partial unique index + index。Down 對稱。

- [ ] **Step 1.1: 寫 migration up + down**

Create `alembic/versions/20260522_acadhk01_academic_term_is_current_and_leave_quota_school_year.py`:

```python
"""academic_term is_current and leave_quota school_year

Revision ID: acadhk01
Revises: rfunnel01
Create Date: 2026-05-22

Schema 增量，無 data migration。
- academic_terms.is_current: 目前學期 flag，partial unique singleton
- leave_quotas.school_year: 民國學年；nullable（共存 legacy year-based row）
"""

from alembic import op
import sqlalchemy as sa


revision = "acadhk01"
down_revision = "rfunnel01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # === AcademicTerm.is_current ===
    if "is_current" not in {c["name"] for c in insp.get_columns("academic_terms")}:
        op.add_column(
            "academic_terms",
            sa.Column(
                "is_current",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )

    existing_idx = {i["name"] for i in insp.get_indexes("academic_terms")}
    if "uq_academic_terms_is_current_singleton" not in existing_idx:
        op.create_index(
            "uq_academic_terms_is_current_singleton",
            "academic_terms",
            ["is_current"],
            unique=True,
            postgresql_where=sa.text("is_current = true"),
            sqlite_where=sa.text("is_current = 1"),
        )

    # === LeaveQuota.school_year ===
    if "school_year" not in {c["name"] for c in insp.get_columns("leave_quotas")}:
        op.add_column(
            "leave_quotas",
            sa.Column("school_year", sa.Integer(), nullable=True),
        )

    existing_idx_lq = {i["name"] for i in insp.get_indexes("leave_quotas")}
    if "uq_leave_quotas_employee_school_year_type" not in existing_idx_lq:
        op.create_index(
            "uq_leave_quotas_employee_school_year_type",
            "leave_quotas",
            ["employee_id", "school_year", "leave_type"],
            unique=True,
            postgresql_where=sa.text("school_year IS NOT NULL"),
            sqlite_where=sa.text("school_year IS NOT NULL"),
        )
    if "ix_leave_quotas_school_year" not in existing_idx_lq:
        op.create_index(
            "ix_leave_quotas_school_year",
            "leave_quotas",
            ["school_year"],
            postgresql_where=sa.text("school_year IS NOT NULL"),
            sqlite_where=sa.text("school_year IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    existing_idx_lq = {i["name"] for i in insp.get_indexes("leave_quotas")}
    if "ix_leave_quotas_school_year" in existing_idx_lq:
        op.drop_index("ix_leave_quotas_school_year", table_name="leave_quotas")
    if "uq_leave_quotas_employee_school_year_type" in existing_idx_lq:
        op.drop_index(
            "uq_leave_quotas_employee_school_year_type",
            table_name="leave_quotas",
        )
    if "school_year" in {c["name"] for c in insp.get_columns("leave_quotas")}:
        op.drop_column("leave_quotas", "school_year")

    existing_idx = {i["name"] for i in insp.get_indexes("academic_terms")}
    if "uq_academic_terms_is_current_singleton" in existing_idx:
        op.drop_index(
            "uq_academic_terms_is_current_singleton",
            table_name="academic_terms",
        )
    if "is_current" in {c["name"] for c in insp.get_columns("academic_terms")}:
        op.drop_column("academic_terms", "is_current")
```

- [ ] **Step 1.2: 跑 migration upgrade**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/recruitment-funnel-phase-a-be
alembic upgrade heads
```
Expected:
```
INFO  [alembic.runtime.migration] Running upgrade rfunnel01 -> acadhk01, academic_term is_current and leave_quota school_year
```

- [ ] **Step 1.3: 跑 migration downgrade 驗對稱**

Run:
```bash
alembic downgrade -1
alembic upgrade heads
```
Expected: 兩次都成功，沒 raise

- [ ] **Step 1.4: SQL 驗欄位 + index 存在**

Run:
```bash
psql -U yilunwu -d ivymanagement -c "\d academic_terms" | grep is_current
psql -U yilunwu -d ivymanagement -c "\d leave_quotas" | grep school_year
psql -U yilunwu -d ivymanagement -c "\di academic_terms" | grep singleton
psql -U yilunwu -d ivymanagement -c "\di leave_quotas" | grep school_year
```
Expected: 四個都有對應 row

- [ ] **Step 1.5: Commit**

```bash
git add alembic/versions/20260522_acadhk01_academic_term_is_current_and_leave_quota_school_year.py
git commit -m "$(cat <<'EOF'
feat(db): academic_terms.is_current + leave_quotas.school_year migration

acadhk01：schema 增量、idempotent up/down、無 data migration。
- academic_terms.is_current: partial unique singleton (postgresql_where + sqlite_where)
- leave_quotas.school_year: nullable，共存 legacy year-based row
- leave_quotas 加 partial unique (employee_id, school_year, leave_type) 與
  ix_leave_quotas_school_year（皆 school_year IS NOT NULL where clause）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Model 層 — `AcademicTerm.is_current` + `LeaveQuota.school_year`

**Files:**
- Modify: `models/academic_term.py`
- Modify: `models/leave.py`

**Goal:** 反映 schema 增量到 ORM 層；新欄位 default 與 partial unique 對齊 migration。

- [ ] **Step 2.1: 改 models/academic_term.py**

替換檔案內容：

```python
"""models/academic_term.py — 學年/學期/開學日設定。

scheduler 在 `start_date` 當天觸發批量推進 enrolled → active。
is_current 用於 admin 顯式翻牌、partial unique 保證 singleton。
"""

from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    Boolean,
    Date,
    DateTime,
    UniqueConstraint,
    CheckConstraint,
    Index,
    text,
)
from models.base import Base


class AcademicTerm(Base):
    __tablename__ = "academic_terms"

    id = Column(Integer, primary_key=True, index=True)
    school_year = Column(Integer, nullable=False, comment="民國學年")
    semester = Column(Integer, nullable=False, comment="1=上學期、2=下學期")
    start_date = Column(Date, nullable=False, comment="開學日")
    end_date = Column(Date, nullable=False)
    is_current = Column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        default=False,
        comment="目前學期旗標；全表至多一筆 true",
    )
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            "school_year", "semester", name="uq_academic_terms_year_semester"
        ),
        CheckConstraint("end_date > start_date", name="ck_academic_terms_date_order"),
        CheckConstraint("semester IN (1, 2)", name="ck_academic_terms_semester_valid"),
        Index(
            "uq_academic_terms_is_current_singleton",
            "is_current",
            unique=True,
            postgresql_where=text("is_current = true"),
            sqlite_where=text("is_current = 1"),
        ),
    )
```

- [ ] **Step 2.2: 改 models/leave.py 的 LeaveQuota class**

Read 既有 LeaveQuota class（grep 找到行號）：
```bash
grep -n "class LeaveQuota" models/leave.py
```

在 LeaveQuota 內加 `school_year` 欄位（其他欄位之間，order 不嚴格）：
```python
    school_year = Column(
        Integer,
        nullable=True,
        comment="民國學年；null = legacy year-based row",
    )
```

在 `__table_args__` tuple 內追加兩個 Index（同 tuple，逗號分隔）：
```python
        Index(
            "uq_leave_quotas_employee_school_year_type",
            "employee_id",
            "school_year",
            "leave_type",
            unique=True,
            postgresql_where=text("school_year IS NOT NULL"),
            sqlite_where=text("school_year IS NOT NULL"),
        ),
        Index(
            "ix_leave_quotas_school_year",
            "school_year",
            postgresql_where=text("school_year IS NOT NULL"),
            sqlite_where=text("school_year IS NOT NULL"),
        ),
```

確認 `from sqlalchemy import ..., Index, text` 已 import（若無則加上）。

- [ ] **Step 2.3: 跑既有測試確認模型載入無誤**

Run:
```bash
pytest tests/test_academic_utils.py tests/test_annual_leave_quota.py -x -q
```
Expected: PASS（不應該因加欄位 break 既有）

- [ ] **Step 2.4: Commit**

```bash
git add models/academic_term.py models/leave.py
git commit -m "$(cat <<'EOF'
feat(models): AcademicTerm.is_current + LeaveQuota.school_year

對齊 acadhk01 migration：
- AcademicTerm.is_current Boolean default false + Index partial unique singleton
- LeaveQuota.school_year Integer nullable + 兩個 partial Index
  (uq_leave_quotas_employee_school_year_type 與 ix_leave_quotas_school_year)

舊 year-based 欄位與 UniqueConstraint 完整保留為 legacy。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `utils/academic.py` 改寫

**Files:**
- Modify: `utils/academic.py`
- Modify: `models/classroom.py`
- Modify: `tests/test_academic_utils.py`

**Goal:** Helper 切成「優先讀 DB、找不到 fallback 日期推算 + warning」；`models/classroom.py` 改 import `default_current_academic_term_for_column` 避免 column default 觸發 DB query。

- [ ] **Step 3.1: 寫 failing test — DB-aware helper**

擴充 `tests/test_academic_utils.py`：

```python
"""utils.academic DB-aware 測試（新增）。

既有測試（純日期推算）保持不動；以下新增 4 個 case 驗證 DB 路徑。
"""

import logging
from datetime import date
from unittest.mock import MagicMock

import pytest

from models.academic_term import AcademicTerm
from utils.academic import (
    resolve_current_academic_term,
    default_current_academic_term_for_column,
)


class TestResolveDBAware:
    def test_resolve_uses_db_is_current_when_set(self, db_session):
        """DB 有 is_current=true row 時回傳該 row 的 (school_year, semester)。"""
        term = AcademicTerm(
            school_year=114,
            semester=2,
            start_date=date(2026, 2, 1),
            end_date=date(2026, 7, 31),
            is_current=True,
        )
        db_session.add(term)
        db_session.flush()

        assert resolve_current_academic_term(session=db_session) == (114, 2)

    def test_resolve_fallback_to_date_when_no_current(self, db_session, caplog):
        """DB 無 is_current=true row 時 fallback 到日期推算 + 寫 warning。"""
        with caplog.at_level(logging.WARNING):
            sy, sem = resolve_current_academic_term(session=db_session)
        # 日期推算應與 _resolve_by_date(date.today()) 一致
        from utils.academic import _resolve_by_date

        assert (sy, sem) == _resolve_by_date(date.today())
        assert any(
            "AcademicTerm.is_current 未設定" in record.message
            for record in caplog.records
        )

    def test_resolve_target_date_skips_db_query(self):
        """顯式傳 target_date 不應查 DB。"""
        mock_session = MagicMock()
        result = resolve_current_academic_term(
            target_date=date(2025, 9, 1), session=mock_session
        )
        assert result == (114, 1)
        mock_session.query.assert_not_called()

    def test_default_for_column_never_queries_db(self):
        """Column default helper 永遠不查 DB（純日期推算）。"""
        # 無 session 參數可傳；如果內部有任何 DB query 會 raise
        result = default_current_academic_term_for_column()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], int)
        assert isinstance(result[1], int)
```

需要 `db_session` fixture — 確認 `conftest.py` 內已有；如沒有，使用既有 `tests/test_activity_academic_term.py` 的 fixture pattern：
```bash
grep -n "db_session\|@pytest.fixture" tests/conftest.py 2>&1 | head -10
```

- [ ] **Step 3.2: 跑 test 確認 fail**

Run:
```bash
pytest tests/test_academic_utils.py::TestResolveDBAware -x -v
```
Expected: 4 個 test 全 FAIL，因 `default_current_academic_term_for_column` 還不存在且 `resolve_current_academic_term` 不接 `session` 參數。

- [ ] **Step 3.3: 改寫 utils/academic.py**

替換檔案內容：

```python
"""共用學年度計算工具。

- resolve_current_academic_term(target_date=None, session=None):
    優先查 AcademicTerm.is_current=true；找不到 fallback 到日期推算 + warning log。
    target_date 顯式傳值時跳過 DB 查詢（用於測試/歷史查詢）。
- default_current_academic_term_for_column():
    SQLAlchemy Column.default 專用、純日期推算、不查 DB。
- resolve_academic_term_filters(school_year, semester, session=None):
    既有介面，多接 session 可選。
- _resolve_by_date(target_date): 私有純函式，原本日期推算邏輯。
- semester_int_to_enum / semester_enum_to_int: 不動。
"""

import logging
from datetime import date
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _resolve_by_date(target_date: date) -> tuple[int, int]:
    """純日期推算學年/學期（民國年）。

    - 8月以後 → 當年上學期（semester=1）
    - 2月~7月 → 前一年下學期（semester=2）
    - 1月 → 前一年上學期（semester=1）
    """
    if target_date.month >= 8:
        return target_date.year - 1911, 1
    if target_date.month >= 2:
        return target_date.year - 1 - 1911, 2
    return target_date.year - 1 - 1911, 1


def resolve_current_academic_term(
    target_date: Optional[date] = None,
    session: Optional[Session] = None,
) -> tuple[int, int]:
    """決定當前學年/學期（民國年）。

    優先順序：
    1. 若顯式傳 target_date → 純日期推算（不查 DB）
    2. 否則查 AcademicTerm.is_current=true，找到就用該 row
    3. 找不到 → fallback _resolve_by_date(date.today()) + logger.warning

    session: caller 已有 session 就傳進來避免 reconnect；不傳則內部開短命 session。
    """
    if target_date is not None:
        return _resolve_by_date(target_date)

    # lazy import 避免 circular（models → utils → models）
    from models.academic_term import AcademicTerm
    from models.base import get_session

    sess = session
    owned = False
    if sess is None:
        sess = get_session()
        owned = True
    try:
        row = (
            sess.query(AcademicTerm)
            .filter(AcademicTerm.is_current.is_(True))
            .first()
        )
        if row:
            return row.school_year, row.semester
        logger.warning(
            "AcademicTerm.is_current 未設定，resolve_current_academic_term() "
            "fallback 到日期推算（請至 /academic-terms UI 設定當前學期）"
        )
        return _resolve_by_date(date.today())
    finally:
        if owned:
            sess.close()


def default_current_academic_term_for_column() -> tuple[int, int]:
    """SQLAlchemy Column.default 專用：純日期推算、不查 DB。

    Classroom._default_school_year/_default_semester 在 INSERT 時呼叫，
    這時候不該觸發 DB query（會在已開 session 內套娃）。
    """
    return _resolve_by_date(date.today())


def resolve_academic_term_filters(
    school_year: Optional[int],
    semester: Optional[int],
    session: Optional[Session] = None,
) -> tuple[int, int]:
    """解析學期篩選參數，未提供時自動使用當前學期；只提供一個時拋 400。"""
    if school_year is None and semester is None:
        return resolve_current_academic_term(session=session)
    if school_year is None or semester is None:
        raise HTTPException(
            status_code=400, detail="school_year 與 semester 需同時提供"
        )
    return school_year, semester


def semester_int_to_enum(sem_int: int):
    """將 1/2 整數轉為 models.appraisal.Semester enum（FIRST/SECOND）。"""
    from models.appraisal import Semester

    if sem_int == 1:
        return Semester.FIRST
    if sem_int == 2:
        return Semester.SECOND
    raise ValueError(f"semester must be 1 or 2, got {sem_int}")


def semester_enum_to_int(sem) -> int:
    """將 models.appraisal.Semester enum 轉為 1/2 整數。"""
    from models.appraisal import Semester

    if sem == Semester.FIRST or sem == "FIRST":
        return 1
    if sem == Semester.SECOND or sem == "SECOND":
        return 2
    raise ValueError(f"semester must be Semester enum, got {sem!r}")
```

- [ ] **Step 3.4: 改 models/classroom.py 接新 helper**

找 `_default_school_year` / `_default_semester` 函式（line 46-53）替換為：

```python
from utils.academic import default_current_academic_term_for_column


def _default_school_year() -> int:
    school_year, _ = default_current_academic_term_for_column()
    return school_year


def _default_semester() -> int:
    _, semester = default_current_academic_term_for_column()
    return semester
```

移除舊的 `from utils.academic import resolve_current_academic_term` import 行（line 23）若該 import 已不被該檔其他位置使用（grep `resolve_current_academic_term` in `models/classroom.py` 驗證）。

- [ ] **Step 3.5: 跑新測試確認通過**

Run:
```bash
pytest tests/test_academic_utils.py -x -v
```
Expected: 既有 6 test + 新增 4 test 全 PASS（10 個）

- [ ] **Step 3.6: 跑既有 20+ caller 對應的 test suite 確認零 regression**

Run:
```bash
pytest tests/test_activity_academic_term.py tests/test_activity_course_phase3.py \
  tests/test_classroom_carry_over.py tests/test_student_profile.py \
  tests/test_activity_pending_review.py -x -q
```
Expected: PASS（`test_classroom_carry_over.py` 還沒建會 collected 0，OK）

- [ ] **Step 3.7: Commit**

```bash
git add utils/academic.py models/classroom.py tests/test_academic_utils.py
git commit -m "$(cat <<'EOF'
refactor(utils): academic.resolve_current_academic_term 查 DB + fallback

- _resolve_by_date(target_date): 私有純函式（原本邏輯內聚）
- resolve_current_academic_term(target_date=None, session=None):
  優先查 AcademicTerm.is_current=true，找不到 fallback 日期推算 + warning log
- default_current_academic_term_for_column():
  SQLAlchemy Column.default 專用，純日期推算（不查 DB 避免 INSERT 副作用）
- models/classroom.py 兩個 column default 改接 default_current_academic_term_for_column

向後相容：
- 既有 caller 不傳 session 走自開短命 session 路徑
- 19 個既有 test 因顯式 target_date 不查 DB，零 test 改動
- 新增 4 個 test 覆蓋 DB-aware 路徑與 column-default safety

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `utils/term_events.py` hooks registry

**Files:**
- Create: `utils/term_events.py`
- Create: `tests/test_term_events.py`

**Goal:** 提供 `@on_term_changed` decorator + `fire_term_changed()` 同步串註派發、`reset_handlers_for_tests()` 測試清空、`register_handler()` ad-hoc 註冊。

- [ ] **Step 4.1: 寫 failing tests**

Create `tests/test_term_events.py`:

```python
"""utils.term_events hooks registry 測試。"""

import pytest
from datetime import date
from unittest.mock import MagicMock

from utils.term_events import (
    on_term_changed,
    register_handler,
    fire_term_changed,
    reset_handlers_for_tests,
    list_handler_names,
)


@pytest.fixture(autouse=True)
def _reset():
    """確保每個 test 從空 registry 開始。"""
    reset_handlers_for_tests()
    yield
    reset_handlers_for_tests()


class _FakeTerm:
    def __init__(self, school_year, semester, id_=1):
        self.id = id_
        self.school_year = school_year
        self.semester = semester


class TestHooksRegistry:
    def test_register_duplicate_raises(self):
        @on_term_changed("dup")
        def h1(*, old, new, session):
            pass

        with pytest.raises(RuntimeError, match="dup"):
            @on_term_changed("dup")
            def h2(*, old, new, session):
                pass

    def test_fire_no_handlers_is_noop(self, caplog):
        new_term = _FakeTerm(115, 1)
        session = MagicMock()
        # 不應 raise
        fire_term_changed(old=None, new=new_term, session=session)

    def test_fire_order_matches_registration(self):
        calls = []

        @on_term_changed("a")
        def h_a(*, old, new, session):
            calls.append("a")

        @on_term_changed("b")
        def h_b(*, old, new, session):
            calls.append("b")

        @on_term_changed("c")
        def h_c(*, old, new, session):
            calls.append("c")

        fire_term_changed(old=None, new=_FakeTerm(115, 1), session=MagicMock())
        assert calls == ["a", "b", "c"]

    def test_fire_handler_raise_propagates_and_stops_chain(self):
        calls = []

        @on_term_changed("first")
        def h1(*, old, new, session):
            calls.append("first")

        @on_term_changed("boom")
        def h2(*, old, new, session):
            calls.append("boom")
            raise ValueError("subscriber failure")

        @on_term_changed("never")
        def h3(*, old, new, session):
            calls.append("never")

        with pytest.raises(ValueError, match="subscriber failure"):
            fire_term_changed(old=None, new=_FakeTerm(115, 1), session=MagicMock())
        assert calls == ["first", "boom"]  # 第三個沒跑

    def test_reset_handlers_clears_registry(self):
        @on_term_changed("x")
        def h(*, old, new, session):
            pass

        assert "x" in list_handler_names()
        reset_handlers_for_tests()
        assert list_handler_names() == []

    def test_register_handler_explicit_api(self):
        called = []

        def manual(*, old, new, session):
            called.append(True)

        register_handler("manual", manual)
        fire_term_changed(old=None, new=_FakeTerm(115, 1), session=MagicMock())
        assert called == [True]
```

- [ ] **Step 4.2: 跑 test 確認 fail（module not found）**

Run:
```bash
pytest tests/test_term_events.py -x -v
```
Expected: FAIL — `ImportError: cannot import name 'on_term_changed' from 'utils.term_events'`

- [ ] **Step 4.3: 寫 utils/term_events.py**

Create `utils/term_events.py`:

```python
"""term.changed 事件 hooks registry。

設計原則：
- 同步、in-process、單一 transaction：caller 持 session、未 commit；
  fire 後依序串註呼叫所有 handler，handler 都在 caller session 上寫；
  任一 handler raise → caller responsibility 整 transaction rollback
- 註冊順序穩定：handler 按 register 順序執行；register 在 module import 時跑，
  startup 顯式 import 一次保證順序
- testability：reset_handlers_for_tests() 清空、register_handler() 不靠 decorator
"""

import logging
from typing import Callable, Optional, Protocol

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class TermLike(Protocol):
    """AcademicTerm 介面 — 用 Protocol 避開 circular import。"""

    id: int
    school_year: int
    semester: int


TermChangedHandler = Callable[..., None]  # signature: (*, old, new, session) -> None


_HANDLERS: list[tuple[str, TermChangedHandler]] = []


def on_term_changed(name: str):
    """Decorator：註冊 handler。name 用於 log、debug、duplicate check。

    用法：
        @on_term_changed("classroom_carry_over")
        def handler(*, old, new, session):
            ...
    """

    def decorator(fn: TermChangedHandler) -> TermChangedHandler:
        register_handler(name, fn)
        return fn

    return decorator


def register_handler(name: str, fn: TermChangedHandler) -> None:
    """顯式註冊（不靠 decorator）。重複 name 會 raise，避免 double-register。"""
    if any(n == name for n, _ in _HANDLERS):
        raise RuntimeError(f"term.changed handler 已註冊：{name}")
    _HANDLERS.append((name, fn))


def fire_term_changed(
    *,
    old: Optional[TermLike],
    new: TermLike,
    session: Session,
) -> None:
    """同步串註呼叫所有 handler。

    Caller contract：
    - 已持有 session 且 transaction 進行中（未 commit）
    - 已完成 AcademicTerm.is_current toggle 的 UPDATE（handler 可看到 new state）
    - handler raise propagate 到 caller 觸發 rollback
    """
    if not _HANDLERS:
        logger.info("term.changed fired but no handler registered")
        return
    for name, handler in _HANDLERS:
        logger.info(
            "term.changed handler 觸發：%s (old=%s, new=%s/%s)",
            name,
            f"{old.school_year}-{old.semester}" if old else None,
            new.school_year,
            new.semester,
        )
        handler(old=old, new=new, session=session)


def reset_handlers_for_tests() -> None:
    """測試專用：清空 handler 註冊表。"""
    _HANDLERS.clear()


def list_handler_names() -> list[str]:
    """debug / 健康檢查用：回傳已註冊 handler 名稱列表。"""
    return [n for n, _ in _HANDLERS]
```

- [ ] **Step 4.4: 跑 test 確認 PASS**

Run:
```bash
pytest tests/test_term_events.py -x -v
```
Expected: 6 test 全 PASS

- [ ] **Step 4.5: Commit**

```bash
git add utils/term_events.py tests/test_term_events.py
git commit -m "$(cat <<'EOF'
feat(utils): term_events hooks registry

提供 term.changed 事件同步 in-process hooks registry。
- @on_term_changed(name) decorator + register_handler() 顯式 API
- fire_term_changed(*, old, new, session) 依註冊順序串註派發
- reset_handlers_for_tests() / list_handler_names() 測試與健康檢查
- 重複註冊同名 handler 直接 raise RuntimeError，避免 startup double-register

Caller contract：handler 都在 caller session 上寫、未 commit；任一 raise 由
caller dep（get_session_dep）觸發整 transaction rollback。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Toggle endpoint `POST /academic-terms/{id}/set-current`

**Files:**
- Modify: `api/academic_terms.py`

**Goal:** Admin 翻牌 endpoint，加在既有 `/academic-terms` router 內，沿用 `SETTINGS_WRITE` 守衛；toggle 完即 `fire_term_changed()`。

- [ ] **Step 5.1: 加 endpoint 到 api/academic_terms.py**

在檔案 import 區補：

```python
from utils.term_events import fire_term_changed
```

在最末（既有 PUT/DELETE 之後）加：

```python
@router.post("/{term_id}/set-current", response_model=AcademicTermOut)
def set_current_term(
    term_id: int,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
) -> AcademicTerm:
    """admin「正式開新學期」翻牌。

    流程（同 transaction）：
    1. 找 new term (term_id) — 不存在 → 404
    2. 找舊 is_current term (可能 None) — 與 new term 相同 → 409 no-op
    3. UPDATE 舊 row.is_current=false（若有），UPDATE new row.is_current=true
    4. flush 讓 partial unique index 立刻檢查 singleton
    5. fire_term_changed(old, new, session) — 三個 subscriber 同 session 串註執行
    """
    new_term = (
        session.query(AcademicTerm).filter(AcademicTerm.id == term_id).first()
    )
    if not new_term:
        raise HTTPException(404, detail="學年學期設定不存在")

    old_term = (
        session.query(AcademicTerm)
        .filter(AcademicTerm.is_current.is_(True))
        .first()
    )
    if old_term and old_term.id == new_term.id:
        raise HTTPException(409, detail="已是目前學期，無需切換")

    if old_term:
        old_term.is_current = False
    new_term.is_current = True
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            500, detail="is_current singleton 違反，請聯絡管理員"
        ) from exc

    logger.info(
        "學期切換：%s → %s（操作者 user_id=%s）",
        f"{old_term.school_year}-{old_term.semester}" if old_term else "(none)",
        f"{new_term.school_year}-{new_term.semester}",
        current_user.get("user_id"),
    )

    fire_term_changed(old=old_term, new=new_term, session=session)

    session.refresh(new_term)
    return new_term
```

- [ ] **Step 5.2: 跑既有 academic_terms 相關 test 確認零 regression**

Run:
```bash
pytest tests/ -k "academic_term" -x -v
```
Expected: 既有 test 全 PASS（新 endpoint 還沒 integration test，OK）

- [ ] **Step 5.3: Commit**

```bash
git add api/academic_terms.py
git commit -m "$(cat <<'EOF'
feat(api): POST /academic-terms/{id}/set-current toggle endpoint

admin「正式開新學期」翻牌端點：
- 同 transaction 內 UPDATE 舊 is_current=false、新 is_current=true、flush
- partial unique index uq_academic_terms_is_current_singleton 保證 singleton
- 完成翻牌後 fire_term_changed() 派發三個 subscriber
- 404 term not found / 409 已是目前學期 / 500 singleton 違反三個 guard

沿用既有 SETTINGS_WRITE 守衛、get_session_dep transaction wrap。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Subscriber 1 — `classroom_carry_over`

**Files:**
- Create: `services/term_subscribers/__init__.py`
- Create: `services/term_subscribers/classroom_carry_over.py`
- Create: `tests/test_classroom_carry_over.py`

**Goal:** 同學年 1→2 自動複製 classroom + 遷移 active student；跨學年 / 非典型切換 no-op + log。

- [ ] **Step 6.1: 建 package marker**

Run:
```bash
mkdir -p services/term_subscribers
touch services/term_subscribers/__init__.py
```

- [ ] **Step 6.2: 寫 failing tests**

Create `tests/test_classroom_carry_over.py`:

```python
"""classroom_carry_over subscriber 單元測試。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Classroom, Student
from models.academic_term import AcademicTerm
from services.term_subscribers.classroom_carry_over import handle
from utils.term_events import reset_handlers_for_tests


@pytest.fixture
def db_session(tmp_path):
    """SQLite in-memory test session（swap base_module 全域 engine pattern）。"""
    db_path = tmp_path / "term.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    session = session_factory()
    yield session
    session.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture(autouse=True)
def _reset():
    reset_handlers_for_tests()
    yield
    reset_handlers_for_tests()


def _make_term(db_session, sy, sem, sd, ed, is_current=False):
    t = AcademicTerm(
        school_year=sy, semester=sem,
        start_date=sd, end_date=ed,
        is_current=is_current,
    )
    db_session.add(t)
    db_session.flush()
    return t


def _make_classroom(db_session, sy, sem, name="ABC"):
    cls = Classroom(
        name=name, school_year=sy, semester=sem,
        capacity=30,
    )
    db_session.add(cls)
    db_session.flush()
    return cls


def _make_student(db_session, classroom_id, student_id, is_active=True):
    s = Student(
        student_id=student_id,
        name=f"學生{student_id}",
        gender="M",
        birthday=date(2020, 1, 1),
        classroom_id=classroom_id,
        is_active=is_active,
    )
    db_session.add(s)
    db_session.flush()
    return s


class TestClassroomCarryOver:
    def test_initial_set_current_no_op(self, db_session):
        """old=None：跳過 carry-over，no exception。"""
        new = _make_term(db_session, 114, 1, date(2025, 8, 1), date(2026, 1, 31), True)
        handle(old=None, new=new, session=db_session)
        # 無 classroom 被建
        assert db_session.query(Classroom).count() == 0

    def test_same_year_1_to_2_copies_classroom_and_moves_students(self, db_session):
        """同學年 1→2：classroom 複製 + active student 遷移。"""
        old = _make_term(db_session, 114, 1, date(2025, 8, 1), date(2026, 1, 31))
        new = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        cls = _make_classroom(db_session, 114, 1, name="星星班")
        s1 = _make_student(db_session, cls.id, "114-A-01", is_active=True)
        s2 = _make_student(db_session, cls.id, "114-A-02", is_active=True)
        inactive = _make_student(db_session, cls.id, "114-A-03", is_active=False)

        handle(old=old, new=new, session=db_session)

        # 新 classroom 建出
        new_classrooms = (
            db_session.query(Classroom)
            .filter(Classroom.school_year == 114, Classroom.semester == 2)
            .all()
        )
        assert len(new_classrooms) == 1
        new_cls = new_classrooms[0]
        assert new_cls.name == "星星班"
        assert new_cls.id != cls.id

        # active student 遷移
        db_session.refresh(s1)
        db_session.refresh(s2)
        db_session.refresh(inactive)
        assert s1.classroom_id == new_cls.id
        assert s2.classroom_id == new_cls.id
        # inactive 不遷移
        assert inactive.classroom_id == cls.id

    def test_cross_year_2_to_1_no_op(self, db_session, caplog):
        """跨學年 114-2 → 115-1：classroom 不動 + log info。"""
        import logging

        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        cls = _make_classroom(db_session, 114, 2, name="月亮班")

        with caplog.at_level(logging.INFO):
            handle(old=old, new=new, session=db_session)

        # 新學期沒有 classroom 被建
        assert (
            db_session.query(Classroom)
            .filter(Classroom.school_year == 115)
            .count() == 0
        )
        assert any("跨學年" in r.message for r in caplog.records)

    def test_empty_old_term_classrooms_noop(self, db_session):
        """上學期 0 classroom 時 early return。"""
        old = _make_term(db_session, 114, 1, date(2025, 8, 1), date(2026, 1, 31))
        new = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        # 沒有 classroom
        handle(old=old, new=new, session=db_session)
        assert db_session.query(Classroom).count() == 0

    def test_atypical_jump_logs_warning(self, db_session, caplog):
        """跳級切換 113-2 → 115-1：no-op + warning log。"""
        import logging

        old = _make_term(db_session, 113, 2, date(2025, 2, 1), date(2025, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))

        with caplog.at_level(logging.WARNING):
            handle(old=old, new=new, session=db_session)

        assert any("非典型切換" in r.message for r in caplog.records)

    def test_copies_classroom_full_fields(self, db_session):
        """複製 classroom 時 head/assistant/art teacher、grade_id、capacity、class_code 都帶過去。"""
        old = _make_term(db_session, 114, 1, date(2025, 8, 1), date(2026, 1, 31))
        new = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))

        cls = Classroom(
            name="大象班", school_year=114, semester=1,
            capacity=25, head_teacher_id=None, assistant_teacher_id=None,
            art_teacher_id=None, grade_id=None, class_code="ELE",
        )
        db_session.add(cls)
        db_session.flush()

        handle(old=old, new=new, session=db_session)

        new_cls = (
            db_session.query(Classroom)
            .filter(Classroom.school_year == 114, Classroom.semester == 2)
            .first()
        )
        assert new_cls.name == "大象班"
        assert new_cls.capacity == 25
        assert new_cls.class_code == "ELE"
```

- [ ] **Step 6.3: 跑 test 確認 fail**

Run:
```bash
pytest tests/test_classroom_carry_over.py -x -v
```
Expected: FAIL — `ImportError: cannot import name 'handle' from 'services.term_subscribers.classroom_carry_over'`

- [ ] **Step 6.4: 寫 services/term_subscribers/classroom_carry_over.py**

Create `services/term_subscribers/classroom_carry_over.py`:

```python
"""term.changed subscriber：同學年 1→2 carry-over Classroom rows 與 Student.classroom_id。

行為矩陣：
- old=None：跳過（初次設定）+ log info
- same school_year semester 1→2：carry-over 全部 active classroom + 學生
- 跨 school_year（X-2 → X+1-1）：no-op + log info（admin 手動編班）
- 其他（2→1 同年 / 大跳）：no-op + log warning（應由 admin 確認異常切換）
"""

import logging
from sqlalchemy.orm import Session

from models.academic_term import AcademicTerm
from models.classroom import Classroom
from models.student import Student
from utils.term_events import on_term_changed

logger = logging.getLogger(__name__)


@on_term_changed("classroom_carry_over")
def handle(
    *, old: AcademicTerm | None, new: AcademicTerm, session: Session
) -> None:
    if old is None:
        logger.info("classroom_carry_over: 初次設定 is_current，跳過 carry-over")
        return

    if (
        old.school_year == new.school_year
        and old.semester == 1
        and new.semester == 2
    ):
        _carry_over_same_year(old, new, session)
        return

    if (
        old.school_year + 1 == new.school_year
        and old.semester == 2
        and new.semester == 1
    ):
        logger.info(
            "classroom_carry_over: 跨學年 %s-2 → %s-1，no-op（請 admin 手動編班升級）",
            old.school_year,
            new.school_year,
        )
        return

    logger.warning(
        "classroom_carry_over: 非典型切換 %s-%s → %s-%s，no-op",
        old.school_year,
        old.semester,
        new.school_year,
        new.semester,
    )


def _carry_over_same_year(
    old: AcademicTerm, new: AcademicTerm, session: Session
) -> None:
    """同學年 1→2：每個 old classroom 生新 row（複製欄位、新 id），
    再把該 classroom 名下 active student.classroom_id 重新指向新 row。"""
    old_classrooms = (
        session.query(Classroom)
        .filter(
            Classroom.school_year == old.school_year,
            Classroom.semester == old.semester,
        )
        .all()
    )
    if not old_classrooms:
        logger.info("classroom_carry_over: 上學期沒有 classroom，跳過")
        return

    old_to_new: dict[int, int] = {}
    for old_cls in old_classrooms:
        new_cls = Classroom(
            name=old_cls.name,
            school_year=new.school_year,
            semester=new.semester,
            grade_id=old_cls.grade_id,
            capacity=old_cls.capacity,
            head_teacher_id=old_cls.head_teacher_id,
            assistant_teacher_id=old_cls.assistant_teacher_id,
            art_teacher_id=old_cls.art_teacher_id,
            class_code=old_cls.class_code,
        )
        session.add(new_cls)
        session.flush()
        old_to_new[old_cls.id] = new_cls.id

    moved = 0
    for old_id, new_id in old_to_new.items():
        result = (
            session.query(Student)
            .filter(
                Student.classroom_id == old_id,
                Student.is_active.is_(True),
            )
            .update(
                {Student.classroom_id: new_id}, synchronize_session=False
            )
        )
        moved += result

    logger.info(
        "classroom_carry_over: 複製 %d 個班級，遷移 %d 位學生 (%s-%s → %s-%s)",
        len(old_classrooms),
        moved,
        old.school_year,
        old.semester,
        new.school_year,
        new.semester,
    )
```

- [ ] **Step 6.5: 跑 test 確認 PASS**

Run:
```bash
pytest tests/test_classroom_carry_over.py -x -v
```
Expected: 6 test 全 PASS

- [ ] **Step 6.6: Commit**

```bash
git add services/term_subscribers/__init__.py \
       services/term_subscribers/classroom_carry_over.py \
       tests/test_classroom_carry_over.py
git commit -m "$(cat <<'EOF'
feat(services): classroom_carry_over term.changed subscriber

同學年 1→2 自動複製 Classroom row 與遷移 active Student.classroom_id；
跨學年（X-2 → X+1-1）no-op + info（admin 手動編班）；非典型切換 warning。

複製欄位：name / grade_id / capacity / head_teacher_id /
assistant_teacher_id / art_teacher_id / class_code。
不遷移 is_active=false 學生（保留為歷史紀錄）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Subscriber 2 — `leave_quota_cutover`

**Files:**
- Create: `services/term_subscribers/leave_quota_cutover.py`
- Create: `tests/test_leave_quota_cutover.py`
- Modify: `api/leaves_quota.py`（擴 `_calc_annual_leave_hours`）

**Goal:** 跨學年（下→新上）為每位 active 員工生 new `school_year`-tagged `leave_quotas` row；特休按 hire_date→new.start_date 年資算；補休 carry-over 結餘；其他法定固定值。Idempotent。

- [ ] **Step 7.1: 擴 `_calc_annual_leave_hours` 簽章**

修改 `api/leaves_quota.py:154`：

```python
def _calc_annual_leave_hours(
    hire_date: "date | None",
    year: int,
    reference_date: "date | None" = None,
) -> float:
    """依勞基法第38條計算特休配額時數。

    reference_date 未提供時 fallback 為 date(year, 12, 31)（向後相容既有 caller）。
    leave_quota_cutover handler 顯式傳入 new_term.start_date。
    """
    if hire_date is None:
        return 0.0
    ref = reference_date or date(year, 12, 31)
    if hire_date > ref:
        return 0.0

    # 完整月數（不足月不計）
    months = (ref.year - hire_date.year) * 12 + (ref.month - hire_date.month)
    if ref.day < hire_date.day:
        months -= 1

    if months < 6:
        return 0.0
    elif months < 12:
        return 24.0  # 3天
    complete_years = months // 12
    if complete_years < 2:
        return 56.0  # 7天
    elif complete_years < 3:
        return 80.0  # 10天
    elif complete_years < 5:
        return 112.0  # 14天
    elif complete_years < 10:
        return 120.0  # 15天
    else:
        days = min(15 + complete_years - 10, 30)
        return float(days * 8)
```

- [ ] **Step 7.2: 跑既有 annual leave quota test 確認零 regression**

Run:
```bash
pytest tests/test_annual_leave_quota.py -x -q
```
Expected: PASS（既有 caller 不傳 `reference_date`，行為等同）

- [ ] **Step 7.3: 寫 failing tests**

Create `tests/test_leave_quota_cutover.py`:

```python
"""leave_quota_cutover subscriber 單元測試。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, LeaveQuota, LeaveRecord
from models.academic_term import AcademicTerm
from services.term_subscribers.leave_quota_cutover import handle
from utils.term_events import reset_handlers_for_tests


@pytest.fixture
def db_session(tmp_path):
    """SQLite in-memory test session（swap base_module 全域 engine pattern）。"""
    db_path = tmp_path / "term.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    session = session_factory()
    yield session
    session.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture(autouse=True)
def _reset():
    reset_handlers_for_tests()
    yield
    reset_handlers_for_tests()


def _make_term(db_session, sy, sem, sd, ed, is_current=False):
    t = AcademicTerm(
        school_year=sy, semester=sem, start_date=sd, end_date=ed,
        is_current=is_current,
    )
    db_session.add(t)
    db_session.flush()
    return t


def _make_emp(db_session, name="員工A", hire_date=date(2020, 9, 1), is_active=True):
    e = Employee(name=name, hire_date=hire_date, is_active=is_active)
    db_session.add(e)
    db_session.flush()
    return e


class TestLeaveQuotaCutover:
    def test_initial_set_current_no_op(self, db_session):
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31), True)
        handle(old=None, new=new, session=db_session)
        assert db_session.query(LeaveQuota).count() == 0

    def test_same_year_no_op(self, db_session):
        """同學年 1→2 不 cutover quota。"""
        old = _make_term(db_session, 114, 1, date(2025, 8, 1), date(2026, 1, 31))
        new = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        _make_emp(db_session)
        handle(old=old, new=new, session=db_session)
        assert db_session.query(LeaveQuota).count() == 0

    def test_cross_year_creates_new_row_for_each_active_employee(self, db_session):
        """跨學年 114-2 → 115-1：每位 active 員工生 6 種假別 new row。"""
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        emp = _make_emp(db_session, hire_date=date(2020, 9, 1))

        handle(old=old, new=new, session=db_session)

        rows = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
            )
            .all()
        )
        # QUOTA_LEAVE_TYPES = {annual, sick, menstrual, personal, family_care} + compensatory
        types = {r.leave_type for r in rows}
        assert types == {"annual", "sick", "menstrual", "personal",
                         "family_care", "compensatory"}

    def test_annual_uses_new_term_start_date_as_reference(self, db_session):
        """特休 reference = new.start_date：2020/9/1 入職、2026/8/1 翻牌 → 年資 ~6年 → 120小時。"""
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        emp = _make_emp(db_session, hire_date=date(2020, 9, 1))

        handle(old=old, new=new, session=db_session)

        annual = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "annual",
            )
            .first()
        )
        # 2020/9/1 → 2026/8/1 = 5 年 11 個月 = 5 完整年 → 14 天 = 112 小時
        # (complete_years=5 進入 elif complete_years < 10 區段 → 120)
        # 仔細算：months=71，complete_years=5，<10 區間 → 120 小時
        assert annual.total_hours == 120.0

    def test_hire_date_none_yields_zero_annual(self, db_session):
        """hire_date 為 None：annual quota 0、note 提示。"""
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        emp = _make_emp(db_session, hire_date=None)

        handle(old=old, new=new, session=db_session)

        annual = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "annual",
            )
            .first()
        )
        assert annual.total_hours == 0.0

    def test_inactive_employee_no_quota_created(self, db_session):
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        _make_emp(db_session, name="離職員工", is_active=False)
        _make_emp(db_session, name="在職員工", is_active=True)

        handle(old=old, new=new, session=db_session)

        assert (
            db_session.query(LeaveQuota)
            .filter(LeaveQuota.school_year == 115)
            .count() == 6  # 只有在職員工的 6 筆
        )

    def test_compensatory_balance_carry_over(self, db_session):
        """補休結餘 carry-over：舊 row 16h、已核准用 6h → 新 row 10h。"""
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        emp = _make_emp(db_session)

        # 舊學年補休 quota row（school_year=114）
        old_comp = LeaveQuota(
            employee_id=emp.id, year=2026, school_year=114,
            leave_type="compensatory", total_hours=16.0,
        )
        db_session.add(old_comp)
        # 已核准請假 6 小時，在 old term 區間內
        used = LeaveRecord(
            employee_id=emp.id, leave_type="compensatory",
            start_date=date(2026, 3, 1), end_date=date(2026, 3, 1),
            leave_hours=6.0, is_approved=True,
        )
        db_session.add(used)
        db_session.flush()

        handle(old=old, new=new, session=db_session)

        new_comp = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "compensatory",
            )
            .first()
        )
        assert new_comp.total_hours == pytest.approx(10.0)

    def test_compensatory_cold_start_falls_back_to_legacy_year_row(self, db_session):
        """First toggle 時系統內只有 legacy year-only row → fallback 查到、結餘正確 carry-over。

        Cold-start 真實情境：cutover 前 system 從未 emit term.changed，
        所有 leave_quotas row 都是 school_year=NULL + year=西元年。
        若 _calc_compensatory_balance 只查 school_year=old.school_year，會找不到 row、
        全員補休 silently 變 0 = P0 data-loss bug。
        """
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        emp = _make_emp(db_session)

        # 模擬 first cutover 前的狀態：只有 legacy year-only row（school_year=NULL）
        legacy = LeaveQuota(
            employee_id=emp.id, year=2026, school_year=None,
            leave_type="compensatory", total_hours=20.0,
        )
        db_session.add(legacy)
        db_session.flush()

        handle(old=old, new=new, session=db_session)

        new_comp = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "compensatory",
            )
            .first()
        )
        # 結餘應從 legacy row 拿到（20h），沒被誤判為 0
        assert new_comp.total_hours == pytest.approx(20.0)
        assert "carry-over" in (new_comp.note or "")

    def test_idempotent_repeated_handle(self, db_session):
        """同 school_year row 已存在則 skip，不會 double-insert。"""
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        emp = _make_emp(db_session)

        handle(old=old, new=new, session=db_session)
        first_count = (
            db_session.query(LeaveQuota)
            .filter(LeaveQuota.school_year == 115)
            .count()
        )

        # 第二次跑（admin 不小心連按）
        handle(old=old, new=new, session=db_session)
        second_count = (
            db_session.query(LeaveQuota)
            .filter(LeaveQuota.school_year == 115)
            .count()
        )
        assert first_count == second_count
```

- [ ] **Step 7.4: 跑 test 確認 fail**

Run:
```bash
pytest tests/test_leave_quota_cutover.py -x -v
```
Expected: FAIL — module not found

- [ ] **Step 7.5: 寫 services/term_subscribers/leave_quota_cutover.py**

Create `services/term_subscribers/leave_quota_cutover.py`:

```python
"""term.changed subscriber：跨學年（下→新學年上）為員工生 leave_quotas new row。

行為矩陣：
- old=None：跳過 + log info
- same school_year (1→2)：no-op + log info（quota 不按學期切換、按學年）
- 跨學年（X-2 → X+1-1）：為每位 active 員工 INSERT new row with school_year=X+1
  - annual: 依 hire_date → new_term.start_date 年資套勞基法第 38 條
  - QUOTA_LEAVE_TYPES 其他: STATUTORY_QUOTA_HOURS
  - compensatory: 結餘 carry-over (舊 row.total_hours - approved_used_in_old_term)
- 其他切換: no-op + log info
- idempotent: pre-check school_year row 已存在則 skip
"""

import logging
from sqlalchemy import func
from sqlalchemy.orm import Session

from models.academic_term import AcademicTerm
from models.employee import Employee
from models.leave import LeaveQuota, LeaveRecord
from utils.term_events import on_term_changed
from api.leaves_quota import (
    QUOTA_LEAVE_TYPES,
    STATUTORY_QUOTA_HOURS,
    _calc_annual_leave_hours,
)

logger = logging.getLogger(__name__)


@on_term_changed("leave_quota_cutover")
def handle(
    *, old: AcademicTerm | None, new: AcademicTerm, session: Session
) -> None:
    if old is None:
        logger.info("leave_quota_cutover: 初次設定 is_current，跳過 cutover")
        return

    if not (
        old.school_year + 1 == new.school_year
        and old.semester == 2
        and new.semester == 1
    ):
        logger.info(
            "leave_quota_cutover: 非跨學年切換 (%s-%s → %s-%s)，no-op",
            old.school_year,
            old.semester,
            new.school_year,
            new.semester,
        )
        return

    _cutover_for_all_active_employees(old, new, session)


def _cutover_for_all_active_employees(
    old: AcademicTerm, new: AcademicTerm, session: Session
) -> None:
    """為每位 active 員工 INSERT 6 種假別 new leave_quotas row with school_year=new.school_year。"""
    active_emps = (
        session.query(Employee).filter(Employee.is_active.is_(True)).all()
    )

    annual_ref = new.start_date
    new_sy = new.school_year

    created_count = 0
    for emp in active_emps:
        existing_types = {
            r[0]
            for r in (
                session.query(LeaveQuota.leave_type)
                .filter(
                    LeaveQuota.employee_id == emp.id,
                    LeaveQuota.school_year == new_sy,
                )
                .all()
            )
        }

        for lt in QUOTA_LEAVE_TYPES:
            if lt in existing_types:
                continue
            if lt == "annual":
                hours = _calc_annual_leave_hours(
                    emp.hire_date,
                    year=annual_ref.year,
                    reference_date=annual_ref,
                )
                note = (
                    f"年資 (基準 {annual_ref.isoformat()}) 換算（依勞基法第38條）"
                )
            else:
                hours = STATUTORY_QUOTA_HOURS[lt]
                note = "法定年度上限（學年制）"

            session.add(
                LeaveQuota(
                    employee_id=emp.id,
                    year=annual_ref.year,  # legacy 欄保留同年（供舊 caller 過渡）
                    school_year=new_sy,
                    leave_type=lt,
                    total_hours=hours,
                    note=note,
                )
            )
            created_count += 1

        # 補休（不在 QUOTA_LEAVE_TYPES，但要 carry-over 結餘）
        if "compensatory" not in existing_types:
            balance = _calc_compensatory_balance(emp.id, old, new, session)
            session.add(
                LeaveQuota(
                    employee_id=emp.id,
                    year=annual_ref.year,
                    school_year=new_sy,
                    leave_type="compensatory",
                    total_hours=balance,
                    note=f"上學年結餘 {balance:.1f} 小時 carry-over",
                )
            )
            created_count += 1

    session.flush()
    logger.info(
        "leave_quota_cutover: %d 位員工生 %d 筆 leave_quotas row (school_year=%s)",
        len(active_emps),
        created_count,
        new_sy,
    )


def _calc_compensatory_balance(
    employee_id: int,
    old: AcademicTerm,
    new: AcademicTerm,
    session: Session,
) -> float:
    """補休結餘 = 上學年 row.total_hours - 已核准已用 (篩選 old term 區間)。

    Cold-start 相容：first toggle 時系統內只有 legacy year-only row。
    先按 school_year 查、找不到 fallback 找 (school_year IS NULL AND year=old.start_date.year)
    的 legacy row。避免全員 silently 歸零。
    """
    # 學年 row 優先
    old_quota = (
        session.query(LeaveQuota)
        .filter(
            LeaveQuota.employee_id == employee_id,
            LeaveQuota.school_year == old.school_year,
            LeaveQuota.leave_type == "compensatory",
        )
        .first()
    )
    # Cold-start fallback：legacy year-only row
    if not old_quota:
        old_quota = (
            session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == employee_id,
                LeaveQuota.school_year.is_(None),
                LeaveQuota.year == old.start_date.year,
                LeaveQuota.leave_type == "compensatory",
            )
            .first()
        )
    if not old_quota:
        return 0.0
    approved_used = (
        session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0))
        .filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.leave_type == "compensatory",
            LeaveRecord.is_approved.is_(True),
            LeaveRecord.start_date >= old.start_date,
            LeaveRecord.start_date < new.start_date,
        )
        .scalar()
        or 0.0
    )
    return max(0.0, float(old_quota.total_hours) - float(approved_used))
```

- [ ] **Step 7.6: 跑 test 確認 PASS**

Run:
```bash
pytest tests/test_leave_quota_cutover.py -x -v
```
Expected: 8 test 全 PASS

- [ ] **Step 7.7: Commit**

```bash
git add api/leaves_quota.py \
       services/term_subscribers/leave_quota_cutover.py \
       tests/test_leave_quota_cutover.py
git commit -m "$(cat <<'EOF'
feat(services): leave_quota_cutover term.changed subscriber

跨學年（X-2 → X+1-1）為每位 active 員工生 6 種假別 new leave_quotas row
with school_year=X+1：
- annual：hire_date → new_term.start_date 年資算（勞基法第38條）
- QUOTA_LEAVE_TYPES 其他：STATUTORY_QUOTA_HOURS
- compensatory：上學年 row.total - 已核准已用（篩 old.start_date ~ new.start_date 區間）
  cold-start fallback：找不到 school_year row 時退回 legacy year-only row
  (school_year IS NULL AND year=old.start_date.year)，避免 first toggle 全員補休
  silently 歸零（advisor catch 的 P0 data-loss bug）

同學年 1→2 / 初次設定 / 非典型切換 no-op。
Idempotent：pre-check (employee_id, school_year, leave_type) 已存在則 skip。

_calc_annual_leave_hours 擴 reference_date 參數（向後相容既有 init_leave_quotas）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Subscriber 3 + main.py 註冊

**Files:**
- Create: `services/term_subscribers/activity_semester_tag.py`
- Modify: `main.py`

**Goal:** Placeholder subscriber 預留 hook；`main.py` 在 `on_startup()` 顯式 import 三個 subscriber + sanity log。

- [ ] **Step 8.1: 寫 activity_semester_tag.py**

Create `services/term_subscribers/activity_semester_tag.py`:

```python
"""term.changed subscriber：活動報名學期標籤 reset（placeholder）。

目前 v1：log info 通知，不做實質動作。
未來 v2：清除 ActivityRegistration 「目前學期」標記、或自動把上學期未結算報名歸檔。
"""

import logging
from sqlalchemy.orm import Session

from models.academic_term import AcademicTerm
from utils.term_events import on_term_changed

logger = logging.getLogger(__name__)


@on_term_changed("activity_semester_tag_reset")
def handle(
    *, old: AcademicTerm | None, new: AcademicTerm, session: Session
) -> None:
    logger.info(
        "activity_semester_tag_reset: placeholder triggered for %s-%s → %s-%s "
        "(目前為 no-op，未來實作學期報名標籤更新)",
        old.school_year if old else None,
        old.semester if old else None,
        new.school_year,
        new.semester,
    )
```

- [ ] **Step 8.2: 改 main.py — 在 on_startup() 註冊三個 subscriber**

找到 `def on_startup():`（line ~188），在函式結尾（既有 `init_*_services()` 呼叫之後）加：

```python
    # term.changed handlers 註冊（import-time 觸發 @on_term_changed decorator）
    import services.term_subscribers.classroom_carry_over   # noqa: F401
    import services.term_subscribers.leave_quota_cutover    # noqa: F401
    import services.term_subscribers.activity_semester_tag  # noqa: F401

    from utils.term_events import list_handler_names
    logger.info("term.changed handlers: %s", list_handler_names())
```

> 註：如果 `on_startup()` 已被 `init_*_services()` 在 module top-level（line ~600 附近）呼叫，這段註冊邏輯仍然有效，因為 `on_startup()` 在 lifespan 中也會被呼叫一次。重複 import 是 noop（Python module cache）但 `@on_term_changed` decorator 只在第一次 import 跑，所以 RuntimeError "已註冊" 不會炸。

- [ ] **Step 8.3: 跑 FastAPI startup sanity（uvicorn dry-run）**

Run:
```bash
python -c "from main import app; print('OK')" 2>&1 | tail -5
```
Expected: 不應該 raise；可能會看到 stdout `term.changed handlers: ['classroom_carry_over', 'leave_quota_cutover', 'activity_semester_tag_reset']`

- [ ] **Step 8.4: Commit**

```bash
git add services/term_subscribers/activity_semester_tag.py main.py
git commit -m "$(cat <<'EOF'
feat(services): activity_semester_tag_reset placeholder subscriber

預留 term.changed hook：目前 v1 只 log info，未來實作 ActivityRegistration
學期標籤清除 / 上學期未結算報名歸檔。

main.py:on_startup() 顯式 import 三個 subscriber（classroom_carry_over /
leave_quota_cutover / activity_semester_tag_reset）+ sanity log handler
名稱列表，確保部署後 log 可確認註冊完整。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: `_check_quota` 學年優先讀路徑

**Files:**
- Modify: `api/leaves_quota.py`

**Goal:** 加 `_resolve_quota_row(session, employee_id, leave_type, target_date=None)` helper：先按 `school_year` 查、找不到 fallback 西元年 row。`_check_quota` / `_check_compensatory_quota` 改用此 helper。

- [ ] **Step 9.1: 加 `_resolve_quota_row` helper**

在 `api/leaves_quota.py` 的 helper 區（line ~150 附近，`_calc_annual_leave_hours` 上方或下方）加：

```python
def _resolve_quota_row(
    session,
    employee_id: int,
    leave_type: str,
    *,
    target_date: "date | None" = None,
) -> "LeaveQuota | None":
    """讀 quota row：先按學年（DB-aware）查、找不到 fallback 到西元年 legacy row。

    過渡期相容：新 cutover 後寫 school_year-tagged row、舊 init_leave_quotas
    仍寫 year-only row（school_year=NULL）。讀路徑優先學年、無則 fallback。
    """
    from utils.academic import resolve_current_academic_term

    school_year, _ = resolve_current_academic_term(
        target_date=target_date, session=session
    )
    row = (
        session.query(LeaveQuota)
        .filter(
            LeaveQuota.employee_id == employee_id,
            LeaveQuota.school_year == school_year,
            LeaveQuota.leave_type == leave_type,
        )
        .first()
    )
    if row:
        return row
    legacy_year = (target_date or date.today()).year
    return (
        session.query(LeaveQuota)
        .filter(
            LeaveQuota.employee_id == employee_id,
            LeaveQuota.school_year.is_(None),
            LeaveQuota.year == legacy_year,
            LeaveQuota.leave_type == leave_type,
        )
        .first()
    )
```

- [ ] **Step 9.2: 改 `_check_quota` 用 `_resolve_quota_row`**

找 `_check_quota`（line ~464），替換 query 邏輯（保留主體計算）：

```python
def _check_quota(
    session,
    employee_id: int,
    leave_type: str,
    year: int,
    leave_hours: float,
    exclude_id: int = None,
    include_pending: bool = True,
) -> None:
    """..."""
    if leave_type not in QUOTA_LEAVE_TYPES:
        return

    # 學年優先 + 西元年 fallback（過渡期相容）
    quota = _resolve_quota_row(session, employee_id, leave_type)

    if quota is None:
        return  # 配額未初始化，略過檢查

    # ... 原本 remaining / pending / approved 比對邏輯不變 ...
```

> 註：原本函式內以 `LeaveQuota.year == year` 篩 — 已用 `_resolve_quota_row` 取代。下方的 `_get_approved_hours_in_year(session, employee_id, year, ...)` 與 `_get_pending_hours_in_year` 保留按 `year` 西元年篩（暫不改，spec §8 列為 follow-up）。

- [ ] **Step 9.3: 改 `_check_compensatory_quota` 用 `_resolve_quota_row`**

找 `_check_compensatory_quota`（line ~404），同樣替換 query：

```python
def _check_compensatory_quota(
    session,
    employee_id: int,
    year: int,
    leave_hours: float,
    exclude_id: int = None,
    include_pending: bool = True,
) -> None:
    """補休配額專用檢查（學年優先讀）。"""
    quota = _resolve_quota_row(session, employee_id, "compensatory")
    total = float(quota.total_hours) if quota else 0.0

    # ... 後續 remaining / approved / pending 計算邏輯不變 ...
```

- [ ] **Step 9.4: 跑既有 leaves quota test 確認零 regression**

Run:
```bash
pytest tests/test_annual_leave_quota.py tests/test_portal_compensatory_quota.py \
  tests/test_leaves_overtimes_bug_batch_2026_05_11.py -x -q
```
Expected: PASS（既有 fixture 寫 `school_year=NULL` 的 row，新讀路徑會 fallback 到西元年 row，行為等同）

- [ ] **Step 9.5: 寫補充 test 驗證讀路徑優先順序**

擴 `tests/test_term_change_integration.py`（檔案還沒建，下一個 task 才建；先在這裡加一個獨立 file 或暫時跳過 — 在 Task 10 整合測試裡加 `test_read_path_prefers_school_year_falls_back_to_year`）

- [ ] **Step 9.6: Commit**

```bash
git add api/leaves_quota.py
git commit -m "$(cat <<'EOF'
refactor(leaves_quota): _check_quota 學年優先讀路徑

新增 _resolve_quota_row(session, employee_id, leave_type, target_date=None)：
先按 resolve_current_academic_term() 學年查 school_year-tagged row、找不到
fallback 到西元年 legacy row (school_year IS NULL AND year=current_year)。

_check_quota 與 _check_compensatory_quota 改用此 helper，主體 remaining /
pending / approved 計算邏輯不變。

過渡期相容：cutover 後寫 school_year row、init_leave_quotas 仍寫 year-only
row（spec §8 follow-up：下季清除 legacy fallback 與 _get_approved_hours_in_year
按學年區間篩）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: 整合測試 — `POST /academic-terms/{id}/set-current`

**Files:**
- Create: `tests/test_term_change_integration.py`

**Goal:** 端對端驗證 toggle endpoint + 3 subscriber 同 transaction 串接、404/409、rollback 路徑、idempotent、跳級切換、讀路徑優先順序。

- [ ] **Step 10.1: 寫整合測試**

Create `tests/test_term_change_integration.py`:

```python
"""POST /academic-terms/{id}/set-current 整合測試。

涵蓋 spec §9.2 的 11 個整合 scenario。
"""

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
from api.academic_terms import router as academic_terms_router
from models.database import Base, Classroom, Employee, LeaveQuota, LeaveRecord, Student, User
from models.academic_term import AcademicTerm
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def term_test(tmp_path):
    """整合測試 fixture：TestClient + session factory + admin login headers。

    Returns:
        (client, session_factory, admin_headers) tuple
    """
    db_path = tmp_path / "term_integration.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(academic_terms_router)

    # 確保 subscriber 已 import 並註冊（test 不靠 lifespan 跑 on_startup）
    from utils.term_events import reset_handlers_for_tests
    reset_handlers_for_tests()
    import services.term_subscribers.classroom_carry_over   # noqa: F401
    import services.term_subscribers.leave_quota_cutover    # noqa: F401
    import services.term_subscribers.activity_semester_tag  # noqa: F401

    # 建 admin user
    with session_factory() as s:
        admin = User(
            username="admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permissions=Permission.SETTINGS_READ | Permission.SETTINGS_WRITE,
            is_active=True,
        )
        s.add(admin)
        s.commit()

    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "TempPass123"},
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {token}"}

        yield client, session_factory, admin_headers

    _ip_attempts.clear()
    _account_failures.clear()
    reset_handlers_for_tests()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def client(term_test):
    return term_test[0]


@pytest.fixture
def db_session(term_test):
    """每個 test 內取得 fresh session（與 TestClient 共用 sqlite engine）。"""
    _, session_factory, _ = term_test
    s = session_factory()
    yield s
    s.close()


@pytest.fixture
def admin_headers(term_test):
    return term_test[2]


def _seed_term(session, *, school_year, semester, start_date, end_date):
    """Helper：直接 INSERT term row（繞過 /academic-terms POST 簡化 setup）。"""
    t = AcademicTerm(
        school_year=school_year, semester=semester,
        start_date=start_date, end_date=end_date,
    )
    session.add(t)
    session.flush()
    return t


def _seed_classroom(session, sy, sem, name="ABC"):
    cls = Classroom(name=name, school_year=sy, semester=sem, capacity=30)
    session.add(cls)
    session.flush()
    return cls


def _seed_student(session, classroom_id, student_id):
    s = Student(
        student_id=student_id,
        name=f"S{student_id}",
        gender="M",
        birthday=date(2020, 1, 1),
        classroom_id=classroom_id,
        is_active=True,
    )
    session.add(s)
    session.flush()
    return s


def _seed_emp(session, hire_date=date(2020, 9, 1)):
    e = Employee(name="員工", hire_date=hire_date, is_active=True)
    session.add(e)
    session.flush()
    return e


class TestTermChangeIntegration:
    def test_initial_set_current_no_subscribers_run(
        self, client, db_session, admin_headers
    ):
        """old=None 時 3 subscriber 全 no-op。"""
        t = _seed_term(
            db_session,
            school_year=115, semester=1,
            start_date=date(2026, 8, 1), end_date=date(2027, 1, 31),
        )
        r = client.post(f"/api/academic-terms/{t.id}/set-current",
                        headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["is_current"] is True
        # 沒 classroom 被建、沒 quota 被建
        assert db_session.query(Classroom).count() == 0
        assert db_session.query(LeaveQuota).count() == 0

    def test_same_year_1_to_2_classroom_carry_over(
        self, client, db_session, admin_headers
    ):
        """114-1 → 114-2：classroom 複製、學生遷移、quota 不動。"""
        old_t = _seed_term(
            db_session,
            school_year=114, semester=1,
            start_date=date(2025, 8, 1), end_date=date(2026, 1, 31),
        )
        old_t.is_current = True
        new_t = _seed_term(
            db_session,
            school_year=114, semester=2,
            start_date=date(2026, 2, 1), end_date=date(2026, 7, 31),
        )
        old_cls = _seed_classroom(db_session, 114, 1, name="星星班")
        s = _seed_student(db_session, old_cls.id, "114-A-01")
        db_session.commit()

        r = client.post(f"/api/academic-terms/{new_t.id}/set-current",
                        headers=admin_headers)
        assert r.status_code == 200

        new_cls = (
            db_session.query(Classroom)
            .filter(Classroom.school_year == 114, Classroom.semester == 2)
            .first()
        )
        assert new_cls is not None
        assert new_cls.name == "星星班"
        db_session.refresh(s)
        assert s.classroom_id == new_cls.id
        assert db_session.query(LeaveQuota).count() == 0

    def test_cross_year_2_to_1_leave_quota_cutover(
        self, client, db_session, admin_headers
    ):
        """114-2 → 115-1：classroom 不動、每員工生 new quota row。"""
        old_t = _seed_term(
            db_session,
            school_year=114, semester=2,
            start_date=date(2026, 2, 1), end_date=date(2026, 7, 31),
        )
        old_t.is_current = True
        new_t = _seed_term(
            db_session,
            school_year=115, semester=1,
            start_date=date(2026, 8, 1), end_date=date(2027, 1, 31),
        )
        emp = _seed_emp(db_session)
        db_session.commit()

        r = client.post(f"/api/academic-terms/{new_t.id}/set-current",
                        headers=admin_headers)
        assert r.status_code == 200

        rows = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
            )
            .all()
        )
        assert len(rows) == 6  # 5 QUOTA_LEAVE_TYPES + compensatory

    def test_cross_year_quota_compensatory_balance_carry_over(
        self, client, db_session, admin_headers
    ):
        """補休結餘 carry-over：舊 row 8h、used 2h → 新 row 6h。"""
        from models.leave import LeaveRecord
        old_t = _seed_term(
            db_session,
            school_year=114, semester=2,
            start_date=date(2026, 2, 1), end_date=date(2026, 7, 31),
        )
        old_t.is_current = True
        new_t = _seed_term(
            db_session,
            school_year=115, semester=1,
            start_date=date(2026, 8, 1), end_date=date(2027, 1, 31),
        )
        emp = _seed_emp(db_session)
        # 舊學年 compensatory quota 8h
        db_session.add(LeaveQuota(
            employee_id=emp.id, year=2026, school_year=114,
            leave_type="compensatory", total_hours=8.0,
        ))
        # 已用 2h
        db_session.add(LeaveRecord(
            employee_id=emp.id, leave_type="compensatory",
            start_date=date(2026, 3, 10), end_date=date(2026, 3, 10),
            leave_hours=2.0, is_approved=True,
        ))
        db_session.commit()

        r = client.post(f"/api/academic-terms/{new_t.id}/set-current",
                        headers=admin_headers)
        assert r.status_code == 200

        new_comp = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "compensatory",
            )
            .first()
        )
        assert new_comp.total_hours == pytest.approx(6.0)

    def test_cross_year_annual_uses_new_term_start_date_as_ref(
        self, client, db_session, admin_headers
    ):
        """特休年資 reference = new.start_date。"""
        old_t = _seed_term(
            db_session,
            school_year=114, semester=2,
            start_date=date(2026, 2, 1), end_date=date(2026, 7, 31),
        )
        old_t.is_current = True
        new_t = _seed_term(
            db_session,
            school_year=115, semester=1,
            start_date=date(2026, 8, 1), end_date=date(2027, 1, 31),
        )
        emp = _seed_emp(db_session, hire_date=date(2020, 9, 1))
        db_session.commit()

        client.post(f"/api/academic-terms/{new_t.id}/set-current",
                    headers=admin_headers)

        annual = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "annual",
            )
            .first()
        )
        # 2020/9/1 → 2026/8/1 = 5 完整年 → 120 小時
        assert annual.total_hours == 120.0
        assert "2026-08-01" in (annual.note or "")

    def test_set_current_to_same_term_returns_409(
        self, client, db_session, admin_headers
    ):
        t = _seed_term(
            db_session,
            school_year=115, semester=1,
            start_date=date(2026, 8, 1), end_date=date(2027, 1, 31),
        )
        t.is_current = True
        db_session.commit()

        r = client.post(f"/api/academic-terms/{t.id}/set-current",
                        headers=admin_headers)
        assert r.status_code == 409
        assert "已是目前學期" in r.json()["detail"]

    def test_set_current_to_nonexistent_returns_404(
        self, client, admin_headers
    ):
        r = client.post("/api/academic-terms/99999/set-current",
                        headers=admin_headers)
        assert r.status_code == 404

    def test_handler_raise_rolls_back_entire_transaction(
        self, client, db_session, admin_headers
    ):
        """leave_quota_cutover handler raise → is_current 不變、quota 不建立。

        實作策略：直接 swap _HANDLERS 內 leave_quota_cutover 的 reference 為
        raising stub。@on_term_changed 在 import time 把原 handler 函式 reference
        存進 _HANDLERS list、不靠 lqc.handle 屬性查找，所以 patch.object(lqc, "handle")
        無效（_HANDLERS 仍持有原 function object）；必須直接改 _HANDLERS list
        才能 intercept。
        """
        from utils.term_events import _HANDLERS, register_handler, reset_handlers_for_tests

        old_t = _seed_term(
            db_session,
            school_year=114, semester=2,
            start_date=date(2026, 2, 1), end_date=date(2026, 7, 31),
        )
        old_t.is_current = True
        new_t = _seed_term(
            db_session,
            school_year=115, semester=1,
            start_date=date(2026, 8, 1), end_date=date(2027, 1, 31),
        )
        _seed_emp(db_session)
        db_session.commit()

        # Positive assertion：boom 真的被呼叫，避免 rollback 是因為 handler
        # 根本沒跑（false negative）
        boom_called = []

        def boom(*, old, new, session):
            boom_called.append(True)
            raise RuntimeError("simulated subscriber failure")

        # Snapshot 原本 handlers 後 swap leave_quota_cutover
        original = list(_HANDLERS)
        assert any(n == "leave_quota_cutover" for n, _ in original), (
            "leave_quota_cutover not registered; fixture broken"
        )

        reset_handlers_for_tests()
        for name, fn in original:
            if name == "leave_quota_cutover":
                register_handler(name, boom)
            else:
                register_handler(name, fn)

        try:
            r = client.post(
                f"/api/academic-terms/{new_t.id}/set-current",
                headers=admin_headers,
            )
            # FastAPI 對未捕捉 RuntimeError 預設回 500
            assert r.status_code == 500
            assert boom_called == [True], (
                "boom handler 沒被呼叫 — registry swap 沒生效"
            )
        finally:
            # 必還原 registry，否則污染後續 test
            reset_handlers_for_tests()
            for name, fn in original:
                register_handler(name, fn)

        # is_current 不應變更（rollback 成功的 invariant）
        db_session.expire_all()
        old_after = (
            db_session.query(AcademicTerm)
            .filter(AcademicTerm.id == old_t.id)
            .first()
        )
        new_after = (
            db_session.query(AcademicTerm)
            .filter(AcademicTerm.id == new_t.id)
            .first()
        )
        assert old_after.is_current is True
        assert new_after.is_current is False
        # quota 沒被寫入
        assert db_session.query(LeaveQuota).count() == 0
        # classroom_carry_over 在 boom 前執行；即便有寫入也要被 rollback
        assert (
            db_session.query(Classroom)
            .filter(Classroom.school_year == 115)
            .count() == 0
        )

    def test_idempotent_toggle_does_not_double_insert_quotas(
        self, client, db_session, admin_headers
    ):
        """連按兩次同方向跨學年 → quota row 只有一份。

        現實上第二次按會 409（is_current 已是 new_t），但 handler 內 idempotent
        guard 仍須保證即便 raw call 也不會 double-insert。此處用直接 raw call
        leave_quota_cutover.handle 驗證。
        """
        from services.term_subscribers.leave_quota_cutover import handle as lqc_handle
        old_t = _seed_term(
            db_session,
            school_year=114, semester=2,
            start_date=date(2026, 2, 1), end_date=date(2026, 7, 31),
        )
        new_t = _seed_term(
            db_session,
            school_year=115, semester=1,
            start_date=date(2026, 8, 1), end_date=date(2027, 1, 31),
        )
        _seed_emp(db_session)
        db_session.commit()

        lqc_handle(old=old_t, new=new_t, session=db_session)
        db_session.flush()
        first_count = (
            db_session.query(LeaveQuota)
            .filter(LeaveQuota.school_year == 115)
            .count()
        )
        lqc_handle(old=old_t, new=new_t, session=db_session)
        db_session.flush()
        second_count = (
            db_session.query(LeaveQuota)
            .filter(LeaveQuota.school_year == 115)
            .count()
        )
        assert first_count == second_count == 6

    def test_atypical_jump_113_2_to_115_1_logs_warning_no_op(
        self, client, db_session, admin_headers, caplog
    ):
        """跳級切換 113-2 → 115-1：classroom no-op + warning；quota no-op + info。"""
        import logging
        old_t = _seed_term(
            db_session,
            school_year=113, semester=2,
            start_date=date(2025, 2, 1), end_date=date(2025, 7, 31),
        )
        old_t.is_current = True
        new_t = _seed_term(
            db_session,
            school_year=115, semester=1,
            start_date=date(2026, 8, 1), end_date=date(2027, 1, 31),
        )
        _seed_classroom(db_session, 113, 2)
        db_session.commit()

        with caplog.at_level(logging.WARNING):
            r = client.post(f"/api/academic-terms/{new_t.id}/set-current",
                            headers=admin_headers)
        assert r.status_code == 200
        # classroom 不被複製
        assert (
            db_session.query(Classroom)
            .filter(Classroom.school_year == 115)
            .count() == 0
        )
        # quota 不被建立
        assert db_session.query(LeaveQuota).count() == 0
        # 有 warning 出現
        assert any("非典型切換" in r.message for r in caplog.records)

    def test_read_path_prefers_school_year_falls_back_to_year(
        self, db_session
    ):
        """_resolve_quota_row：school_year row 存在優先、缺則 fallback 西元年。"""
        from api.leaves_quota import _resolve_quota_row

        # 建一筆 is_current term
        t = _seed_term(
            db_session,
            school_year=115, semester=1,
            start_date=date(2026, 8, 1), end_date=date(2027, 1, 31),
        )
        t.is_current = True
        emp = _seed_emp(db_session)

        # 同時建 school_year=115 row 跟 legacy year=2026 row
        new_row = LeaveQuota(
            employee_id=emp.id, year=2026, school_year=115,
            leave_type="annual", total_hours=120.0,
        )
        legacy_row = LeaveQuota(
            employee_id=emp.id, year=2026, school_year=None,
            leave_type="annual", total_hours=100.0,
        )
        db_session.add_all([new_row, legacy_row])
        db_session.flush()

        found = _resolve_quota_row(db_session, emp.id, "annual")
        assert found.id == new_row.id  # 學年優先

        # 刪掉 school_year row → fallback 西元年
        db_session.delete(new_row)
        db_session.flush()
        fallback = _resolve_quota_row(db_session, emp.id, "annual")
        assert fallback.id == legacy_row.id
```

> Fixture pattern：`term_test` 是 file-local（與 `tests/test_activity_academic_term.py:term_client` 同套路），`client` / `db_session` / `admin_headers` 為 derived fixtures。`conftest.py` 不需要動。

- [ ] **Step 10.2: 跑整合測試**

Run:
```bash
pytest tests/test_term_change_integration.py -x -v
```
Expected: 11 個 test 全 PASS

- [ ] **Step 10.3: 全套 regression**

Run:
```bash
pytest tests/ -x -q --ignore=tests/test_audit_router.py --ignore=tests/test_supabase_storage.py
```
Expected: PASS（pre-existing fail 為 test_audit_router 與 test_supabase_storage，與本批無關，已知）

- [ ] **Step 10.4: Commit**

```bash
git add tests/test_term_change_integration.py
git commit -m "$(cat <<'EOF'
test(integration): /academic-terms/{id}/set-current 端對端

涵蓋 spec §9.2 的 11 個整合 scenario：
- old=None 初次設定 3 subscriber no-op
- 同學年 1→2 classroom carry-over 完整路徑
- 跨學年 2→1 leave_quota cutover 完整路徑
- 補休結餘 carry-over 與特休 reference date 驗證
- 409 / 404 / handler raise rollback / 連按 idempotent
- 跳級切換 warning / 讀路徑學年優先 fallback 西元年

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: 整體 verification + push 準備

**Files:** 無新增

**Goal:** 跑完整 pytest 套件、確認 alembic chain 完整、git log 看 11 commit 在預期位置。

- [ ] **Step 11.1: 完整 pytest run**

Run:
```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/recruitment-funnel-phase-a-be
pytest -q 2>&1 | tail -30
```
Expected: 0 new failure；只可能殘留 pre-existing：`tests/test_audit_router.py`（3 fail）+ `tests/test_supabase_storage.py`（6 error），與本批無關。

- [ ] **Step 11.2: Alembic 完整 round-trip**

Run:
```bash
alembic downgrade -2  # 退回 acadhk01 之前
alembic upgrade heads  # 重新升到 acadhk01
alembic history | head -5
```
Expected: `acadhk01 (head)` 在最上、`rfunnel01` 在下；downgrade/upgrade 兩次都成功。

- [ ] **Step 11.3: Git log 驗證 commit 序列**

Run:
```bash
git log --oneline -12
```
Expected：spec commit (`580658b`) + 10 個本批 commit（migration / model / utils / hooks / endpoint / 3 subscriber + main / quota refactor / integration test），共 11 個 new commit。

- [ ] **Step 11.4: 自我審查 — handler 註冊順序 sanity**

Run:
```bash
python -c "
from main import app
from utils.term_events import list_handler_names
print('Registered handlers:', list_handler_names())
assert list_handler_names() == [
    'classroom_carry_over',
    'leave_quota_cutover',
    'activity_semester_tag_reset',
], f'Order mismatch: {list_handler_names()}'
print('Order OK')
"
```
Expected: 看到三個 handler 按預期順序、`Order OK`。

- [ ] **Step 11.5: 通知 user 等手測 + push**

不 push，等 user 手測 worktree 上的 `POST /academic-terms/{id}/set-current` 後手動 merge。

---

## Self-Review Checklist

實作完成前再次檢視：

- [ ] Spec §3 schema 增量 → Task 1 migration + Task 2 model（涵蓋）
- [ ] Spec §4 helper 改寫 → Task 3（涵蓋）
- [ ] Spec §5 term_events → Task 4（涵蓋）
- [ ] Spec §5.1 main.py 註冊 → Task 8 step 8.2（涵蓋）
- [ ] Spec §6 toggle endpoint → Task 5（涵蓋）
- [ ] Spec §7.1 classroom_carry_over → Task 6（涵蓋）
- [ ] Spec §7.2 leave_quota_cutover → Task 7（涵蓋）
- [ ] Spec §7.3 `_calc_annual_leave_hours` 擴 → Task 7 step 7.1（涵蓋）
- [ ] Spec §7.4 activity_semester_tag → Task 8 step 8.1（涵蓋）
- [ ] Spec §8 `_check_quota` 學年優先 → Task 9（涵蓋）
- [ ] Spec §9.1 unit tests → Task 4 / 6 / 7（涵蓋 — `test_term_events` / `test_classroom_carry_over` / `test_leave_quota_cutover`）
- [ ] Spec §9.2 integration tests → Task 10（涵蓋 11 個 scenario）
- [ ] Spec §10 cutover & rollout → 不在 plan，user 手動執行
- [ ] Spec §11 commit 拆分 → Task 1-10 對應 10 個 commit + spec 已 commit = 11 個
- [ ] Spec §12 follow-up → 已標明不在本次範圍

---

## 已知限制

- `test_handler_raise_rolls_back_entire_transaction` 用 `_HANDLERS` swap intercept handler，pattern 較 hacky 但 advisor 確認 `patch.object(lqc, "handle", ...)` 對 `@on_term_changed` 註冊的 reference 無效，必須直接動 list；boom_called positive assertion 防 false-negative
- 預期 status code 為 500（FastAPI 對未捕捉 RuntimeError 預設），若實際 middleware 改寫成 422 或其他，依實測修正
