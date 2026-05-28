# Data Quality Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增每日 03:00 跑的 data quality scheduler，5 條 invariant rule（員工離職未關旗標 / 學生 lifecycle terminal 但 is_active / ContactBook 孤兒 student / Guardian 孤兒 user / SalaryRecord 孤兒 employee），偵測結果 4 線出口（log + 寫表 + Sentry + LINE digest）。前端新增 admin 管理頁面。

**Architecture:** 後端在 `services/data_quality/` 包 engine + dispatch + 5 rules；scheduler 走既有 pattern（`services/data_quality_scheduler.py` async loop + `config/scheduler.py` 加 enabled flag + `try_scheduler_lock` 防多 worker）。dedup 用 partial unique index `(dedup_key) WHERE status='open'` 保證同 entity 同 rule 只一筆 open。前端新增 `DataQualityView.vue` + `src/api/dataQuality.ts`。

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, Sentry SDK, Vue 3 Composition API, Element Plus, Pinia, vitest, openapi-typescript

**Spec:** `docs/superpowers/specs/2026-05-28-observability-forensic-and-design-tokens-design.md` Ch2

**Codebase 校正**（spec 寫的欄位名稱對齊 codebase 實際命名）：
- `Employee` 模型用 `resign_date`，**不是** `offboard_date`
- `Student` 在 `models/classroom.py`（不是 `models/student.py`），含 `is_active` + `lifecycle_status`

---

## File Structure

### 後端 (PR-B)

| 檔案 | 動作 | 責任 |
|---|---|---|
| `alembic/versions/dqreport01_data_quality_reports.py` | Create | 新表 `data_quality_reports` + 3 indexes |
| `models/data_quality.py` | Create | `DataQualityReport` ORM 模型 |
| `models/__init__.py` | Modify | 中央 import 註冊（CLAUDE.md 重點 #5） |
| `utils/permissions.py` | Modify | `Permission.DATA_QUALITY_READ` / `DATA_QUALITY_WRITE` + `PERMISSION_LABELS` + admin/principal `ROLE_TEMPLATES` |
| `services/data_quality/__init__.py` | Create | 空 |
| `services/data_quality/_base.py` | Create | `Violation` NamedTuple + `Rule` ABC + severity 常數 |
| `services/data_quality/rules/employee_offboard.py` | Create | rule code `employee_active_but_offboarded` |
| `services/data_quality/rules/student_stale_active.py` | Create | rule code `student_active_but_lifecycle_terminal` |
| `services/data_quality/rules/contact_book_orphan.py` | Create | rule code `contact_book_orphan_student` |
| `services/data_quality/rules/guardian_orphan_user.py` | Create | rule code `guardian_orphan_user` |
| `services/data_quality/rules/salary_no_employee.py` | Create | rule code `salary_record_orphan_employee` |
| `services/data_quality/engine.py` | Create | `run_all_rules(session) -> list[Violation]` + ALL_RULES registry |
| `services/data_quality/dispatch.py` | Create | `emit(violation, session)` + `flush_line_digest()` |
| `services/data_quality_scheduler.py` | Create | asyncio 包裝（沿用 `finance_reconciliation_scheduler.py` pattern） |
| `config/scheduler.py` | Modify | 加 `data_quality_enabled: BoolEnv = False` + `data_quality_check_interval: int = 60` + `data_quality_hour: int = 3` |
| `main.py` | Modify | startup 注入 `start_data_quality_scheduler` |
| `api/data_quality.py` | Create | 5 endpoint：list / ack / resolve / ignore / run-now |
| `main.py` | Modify | 註冊 `api/data_quality.py:router` |
| `tests/test_data_quality_rules.py` | Create | 5 rule × 各 1-2 個 test |
| `tests/test_data_quality_dispatch.py` | Create | dedup / 4 線 emit |
| `tests/test_data_quality_endpoints.py` | Create | 5 endpoint 測試 |
| `tests/test_data_quality_scheduler.py` | Create | flag off skip / flag on run |

### 前端 (PR-E)

| 檔案 | 動作 | 責任 |
|---|---|---|
| `src/api/_generated/schema.d.ts` | Regen | `npm run gen:api` 拉新 OpenAPI 型別 |
| `src/api/dataQuality.ts` | Create | 5 函式對應 5 endpoint |
| `src/views/DataQualityView.vue` | Create | filter + counter + table + ack/resolve dialog |
| `src/router/index.ts` | Modify | route `/data-quality`，permission gate `DATA_QUALITY_READ` |
| `src/components/layout/MainMenu.vue`（或對應主選單檔） | Modify | 加選單項 |
| `src/utils/permissions.ts` | Modify | `PERMISSION_LABELS` 加 2 項 |
| `src/views/__tests__/DataQualityView.test.ts` | Create | 3 vitest（render / filter / ack action） |

---

## Task 1: Migration dqreport01 + DataQualityReport 模型

**Files:**
- Create: `alembic/versions/dqreport01_data_quality_reports.py`
- Create: `models/data_quality.py`
- Modify: `models/__init__.py`
- Test: `tests/test_data_quality_rules.py`（先建空檔，shema test 入這）

- [ ] **Step 1: 抓 alembic 當前 head**

Run:
```bash
alembic heads
```
記下作為 `down_revision`。**若有多 head 停下回報 user**。

注意：本 plan 應在 Plan 1 (auditfor01) 之後跑，所以 head 預期是 `auditfor01`。

- [ ] **Step 2: 寫 model 失敗測試**

Create `tests/test_data_quality_rules.py`：

```python
"""tests/test_data_quality_rules.py — Ch2 data quality rules + schema."""

from sqlalchemy import inspect

from models.data_quality import DataQualityReport
from models.base import Base


def test_data_quality_report_columns():
    cols = {c.name for c in DataQualityReport.__table__.columns}
    expected = {
        "id", "rule_code", "severity", "entity_type", "entity_id",
        "summary", "detected_at", "last_seen_at", "dedup_key",
        "status", "ack_by", "ack_at", "resolved_at", "resolution_note",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_data_quality_report_registered_in_metadata():
    """CLAUDE.md #5：必須在 models/__init__.py 中央 import 才能進 metadata。"""
    assert "data_quality_reports" in Base.metadata.tables


def test_data_quality_report_has_partial_unique_index():
    """ix_dqr_dedup_open 應為 partial unique (status='open')。"""
    indexes = [idx for idx in DataQualityReport.__table__.indexes]
    open_idx = next(
        (idx for idx in indexes if idx.name == "ix_dqr_dedup_open"),
        None,
    )
    assert open_idx is not None
    assert open_idx.unique is True
```

- [ ] **Step 3: Run failing test**

Run:
```bash
pytest tests/test_data_quality_rules.py -xvs
```
Expected: FAIL — `ModuleNotFoundError: No module named 'models.data_quality'`

- [ ] **Step 4: 建 model**

Create `models/data_quality.py`：

```python
"""models/data_quality.py — Data quality invariant report."""

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)

from models.base import Base
from utils.taipei_time import now_taipei_naive


class DataQualityReport(Base):
    """每日 invariant 偵測結果，狀態：open/ack/fixed/ignored。"""

    __tablename__ = "data_quality_reports"
    __table_args__ = (
        Index("ix_dqr_rule_detected", "rule_code", "detected_at"),
        Index("ix_dqr_status_severity", "status", "severity"),
        Index(
            "ix_dqr_dedup_open",
            "dedup_key",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_code = Column(String(64), nullable=False)
    severity = Column(String(4), nullable=False)  # P0 / P1 / P2
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(String(50), nullable=False)
    summary = Column(Text, nullable=False)
    detected_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    last_seen_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    dedup_key = Column(String(64), nullable=False)
    status = Column(String(10), default="open", nullable=False)
    ack_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    ack_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    resolution_note = Column(Text, nullable=True)
```

- [ ] **Step 5: 中央註冊**

Modify `models/__init__.py`，在既有 model 群集裡加：

```python
from models.data_quality import DataQualityReport  # noqa: F401
```

- [ ] **Step 6: Run schema test pass**

Run:
```bash
pytest tests/test_data_quality_rules.py -xvs
```
Expected: 3 個 PASS

- [ ] **Step 7: 寫 Alembic migration**

Create `alembic/versions/dqreport01_data_quality_reports.py`：

```python
"""data quality reports table

Revision ID: dqreport01
Revises: auditfor01
Create Date: 2026-05-28

Ch2 of observability-forensic-and-design-tokens spec.
新表存放每日 invariant 偵測結果（員工離職未關、學生 lifecycle terminal、
ContactBook 孤兒、Guardian 孤兒、SalaryRecord 孤兒）。
"""

from alembic import op
import sqlalchemy as sa


revision = "dqreport01"
down_revision = "auditfor01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_quality_reports",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("rule_code", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(4), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(50), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column(
            "ack_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("ack_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_dqr_rule_detected",
        "data_quality_reports",
        ["rule_code", "detected_at"],
    )
    op.create_index(
        "ix_dqr_status_severity",
        "data_quality_reports",
        ["status", "severity"],
    )
    # Partial unique index：同 entity 同 rule open 狀態只一筆
    op.execute(
        """
        CREATE UNIQUE INDEX ix_dqr_dedup_open
        ON data_quality_reports (dedup_key)
        WHERE status = 'open';
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dqr_dedup_open;")
    op.drop_index("ix_dqr_status_severity", table_name="data_quality_reports")
    op.drop_index("ix_dqr_rule_detected", table_name="data_quality_reports")
    op.drop_table("data_quality_reports")
```

- [ ] **Step 8: 驗 migration 跑通**

Run:
```bash
alembic upgrade head && alembic downgrade -1 && alembic upgrade head
```
Expected: 全成功，head = `dqreport01`。

- [ ] **Step 9: Commit**

```bash
git add alembic/versions/dqreport01_data_quality_reports.py models/data_quality.py models/__init__.py tests/test_data_quality_rules.py
git commit -m "feat(data-quality): 新表 data_quality_reports + DataQualityReport 模型 (dqreport01)

partial unique index ix_dqr_dedup_open 確保同 entity 同 rule open 狀態只一筆。
模型在 models/__init__.py 中央註冊以進 Base.metadata。
Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch2.1"
```

---

## Task 2: Permission + ROLE_TEMPLATES

**Files:**
- Modify: `utils/permissions.py`
- Test: `tests/test_data_quality_rules.py`

- [ ] **Step 1: 寫 failing test**

加入 `tests/test_data_quality_rules.py`：

```python
from utils.permissions import Permission, PERMISSION_LABELS, ROLE_TEMPLATES


def test_data_quality_permissions_defined():
    assert Permission.DATA_QUALITY_READ.value == "DATA_QUALITY_READ"
    assert Permission.DATA_QUALITY_WRITE.value == "DATA_QUALITY_WRITE"


def test_data_quality_permission_labels_present():
    assert "DATA_QUALITY_READ" in PERMISSION_LABELS
    assert "DATA_QUALITY_WRITE" in PERMISSION_LABELS


def test_admin_role_template_includes_data_quality():
    admin_perms = ROLE_TEMPLATES["admin"]["permissions"]
    assert "DATA_QUALITY_READ" in admin_perms
    assert "DATA_QUALITY_WRITE" in admin_perms


def test_principal_role_template_includes_data_quality():
    principal_perms = ROLE_TEMPLATES["principal"]["permissions"]
    assert "DATA_QUALITY_READ" in principal_perms
    assert "DATA_QUALITY_WRITE" in principal_perms
```

- [ ] **Step 2: Run failing test**

Run:
```bash
pytest tests/test_data_quality_rules.py -k "permission or role_template" -xvs
```
Expected: FAIL — `AttributeError: DATA_QUALITY_READ`

- [ ] **Step 3: 加 Permission 與 labels**

Modify `utils/permissions.py`：

1. 在 `Permission` enum 中加：

```python
    DATA_QUALITY_READ = "DATA_QUALITY_READ"
    DATA_QUALITY_WRITE = "DATA_QUALITY_WRITE"
```

2. 在 `PERMISSION_LABELS` dict 中加：

```python
    "DATA_QUALITY_READ": "資料品質報告 — 檢視",
    "DATA_QUALITY_WRITE": "資料品質報告 — 處理",
```

3. 在 `ROLE_TEMPLATES["admin"]["permissions"]` 與 `ROLE_TEMPLATES["principal"]["permissions"]` 兩 list 各加：

```python
        "DATA_QUALITY_READ",
        "DATA_QUALITY_WRITE",
```

- [ ] **Step 4: Run tests pass**

Run:
```bash
pytest tests/test_data_quality_rules.py -k "permission or role_template" -xvs
```
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add utils/permissions.py tests/test_data_quality_rules.py
git commit -m "feat(perm): Permission.DATA_QUALITY_READ/WRITE + admin/principal templates

Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch2.6"
```

---

## Task 3: Rule base + 第 1 條 rule (employee_active_but_offboarded)

**Files:**
- Create: `services/data_quality/__init__.py`、`_base.py`、`rules/__init__.py`、`rules/employee_offboard.py`
- Test: `tests/test_data_quality_rules.py`

- [ ] **Step 1: 寫 failing test**

加入 `tests/test_data_quality_rules.py`：

```python
from datetime import date, timedelta

from services.data_quality._base import Violation
from services.data_quality.rules.employee_offboard import EmployeeOffboardRule


def test_employee_offboard_rule_detects(db_session_factory):
    """Employee is_active=True 且 resign_date <= today → 1 條 violation。"""
    from models.employee import Employee

    session = db_session_factory()
    emp = Employee(
        employee_id="E999",
        name="測試離職",
        is_active=True,
        resign_date=date.today() - timedelta(days=1),
    )
    session.add(emp)
    session.commit()

    rule = EmployeeOffboardRule()
    violations = rule.check(session)

    assert len(violations) == 1
    v = violations[0]
    assert v.rule_code == "employee_active_but_offboarded"
    assert v.severity == "P1"
    assert v.entity_type == "employee"
    assert v.entity_id == str(emp.id)
    # summary 不含 PII（用 id 不用 name）
    assert "測試離職" not in v.summary
    assert str(emp.id) in v.summary


def test_employee_offboard_rule_skips_inactive(db_session_factory):
    """is_active=False（已關旗標）→ 不偵測。"""
    from models.employee import Employee

    session = db_session_factory()
    emp = Employee(
        employee_id="E998",
        name="正常離職",
        is_active=False,
        resign_date=date.today() - timedelta(days=10),
    )
    session.add(emp)
    session.commit()

    rule = EmployeeOffboardRule()
    assert rule.check(session) == []
```

**Fixture 提示**：`db_session_factory` 是既有 conftest.py 提供的（用法可 grep 既有 test 確認）；若無，用 `from models.database import get_session` 直接連 test DB。

- [ ] **Step 2: Run failing test**

Run:
```bash
pytest tests/test_data_quality_rules.py -k "employee_offboard" -xvs
```
Expected: FAIL — import error。

- [ ] **Step 3: 建 _base + Rule + 第 1 條**

Create `services/data_quality/__init__.py`：

```python
"""services/data_quality/ — 每日 invariant 偵測引擎。"""
```

Create `services/data_quality/_base.py`：

```python
"""Rule abstract + Violation NamedTuple."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from typing import NamedTuple

from sqlalchemy.orm import Session


class Violation(NamedTuple):
    rule_code: str
    severity: str  # "P0" | "P1" | "P2"
    entity_type: str
    entity_id: str
    summary: str

    @property
    def dedup_key(self) -> str:
        raw = f"{self.rule_code}:{self.entity_type}:{self.entity_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


class Rule(ABC):
    """每條 invariant rule 的基類。"""

    code: str = ""
    severity: str = "P2"
    description: str = ""

    @abstractmethod
    def check(self, session: Session) -> list[Violation]:
        ...
```

Create `services/data_quality/rules/__init__.py`（空檔，當 package）：

```python
"""services/data_quality/rules/ — invariant rule implementations."""
```

Create `services/data_quality/rules/employee_offboard.py`：

```python
"""rule: 員工 is_active=True 但 resign_date 已過。"""

from datetime import date

from sqlalchemy.orm import Session

from models.employee import Employee
from services.data_quality._base import Rule, Violation


class EmployeeOffboardRule(Rule):
    code = "employee_active_but_offboarded"
    severity = "P1"
    description = "員工已過離職日但 is_active 仍為 True"

    def check(self, session: Session) -> list[Violation]:
        today = date.today()
        rows = (
            session.query(Employee)
            .filter(
                Employee.is_active.is_(True),
                Employee.resign_date.isnot(None),
                Employee.resign_date <= today,
            )
            .all()
        )
        return [
            Violation(
                rule_code=self.code,
                severity=self.severity,
                entity_type="employee",
                entity_id=str(r.id),
                summary=f"員工 #{r.id} 離職日 {r.resign_date} 已過，is_active 仍為 True",
            )
            for r in rows
        ]
```

- [ ] **Step 4: Run tests pass**

Run:
```bash
pytest tests/test_data_quality_rules.py -k "employee_offboard" -xvs
```
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add services/data_quality/__init__.py services/data_quality/_base.py services/data_quality/rules/__init__.py services/data_quality/rules/employee_offboard.py tests/test_data_quality_rules.py
git commit -m "feat(data-quality): Rule base + employee_active_but_offboarded rule (1/5)

Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch2.2-2.3"
```

---

## Task 4: 加入剩 4 條 rule

**Files:**
- Create: `services/data_quality/rules/student_stale_active.py`、`contact_book_orphan.py`、`guardian_orphan_user.py`、`salary_no_employee.py`
- Test: `tests/test_data_quality_rules.py`

每條 rule 都遵循同樣 TDD 流程：write failing test → implement → run pass → commit（一條 rule 一個 commit）。下方提供每條的 complete code，但流程順序與 Task 3 完全相同。

### 4a: student_active_but_lifecycle_terminal

- [ ] **Step 1: 寫 failing test**

```python
def test_student_stale_active_detects(db_session_factory):
    from models.classroom import Student

    session = db_session_factory()
    s = Student(
        student_id="S999",
        name="畢業學生",
        is_active=True,
        lifecycle_status="GRADUATED",
    )
    session.add(s)
    session.commit()

    from services.data_quality.rules.student_stale_active import StudentStaleActiveRule

    rule = StudentStaleActiveRule()
    violations = rule.check(session)
    assert len(violations) == 1
    assert violations[0].rule_code == "student_active_but_lifecycle_terminal"
    assert violations[0].severity == "P1"
```

- [ ] **Step 2: Run failing test**

```bash
pytest tests/test_data_quality_rules.py -k "student_stale_active" -xvs
```
Expected: FAIL — import error

- [ ] **Step 3: 實作**

Create `services/data_quality/rules/student_stale_active.py`：

```python
"""rule: 學生 lifecycle_status 為終態但 is_active 仍 True。"""

from sqlalchemy.orm import Session

from models.classroom import Student
from services.data_quality._base import Rule, Violation


_TERMINAL_STATUSES = {"GRADUATED", "WITHDRAWN", "TRANSFERRED"}


class StudentStaleActiveRule(Rule):
    code = "student_active_but_lifecycle_terminal"
    severity = "P1"
    description = "學生 lifecycle_status 為終態但 is_active 仍 True"

    def check(self, session: Session) -> list[Violation]:
        rows = (
            session.query(Student)
            .filter(
                Student.is_active.is_(True),
                Student.lifecycle_status.in_(_TERMINAL_STATUSES),
            )
            .all()
        )
        return [
            Violation(
                rule_code=self.code,
                severity=self.severity,
                entity_type="student",
                entity_id=str(r.id),
                summary=f"學生 #{r.id} lifecycle={r.lifecycle_status} 但 is_active 仍 True",
            )
            for r in rows
        ]
```

- [ ] **Step 4: Run pass + commit**

```bash
pytest tests/test_data_quality_rules.py -k "student_stale_active" -xvs
git add services/data_quality/rules/student_stale_active.py tests/test_data_quality_rules.py
git commit -m "feat(data-quality): student_active_but_lifecycle_terminal rule (2/5)"
```

### 4b: contact_book_orphan_student

- [ ] **Step 1: 寫 failing test**

```python
def test_contact_book_orphan_student_detects(db_session_factory):
    from models.contact_book import ContactBookEntry
    from datetime import date

    session = db_session_factory()
    # 直接 INSERT 一筆指向不存在 student_id 的 row（繞 FK，模擬資料異常）
    session.execute(
        text(
            "INSERT INTO contact_book_entries (student_id, log_date, content) "
            "VALUES (:sid, :d, 'orphan')"
        ),
        {"sid": 9999999, "d": date.today()},
    )
    session.commit()

    from services.data_quality.rules.contact_book_orphan import ContactBookOrphanRule

    rule = ContactBookOrphanRule()
    violations = rule.check(session)
    assert any(v.entity_type == "contact_book_entry" for v in violations)
```

**注意**：若 contact_book_entries 表 FK 有強制（PG `REFERENCES`），直接 INSERT 會被擋。對策：(a) 先 INSERT student、(b) 取得 student.id、(c) 刪 student、(d) 此時 contact_book 變孤兒。或測試前 `SET CONSTRAINTS ALL DEFERRED`（如果 PG 是 DEFERRABLE）。

- [ ] **Step 2-4: 實作 + commit**

Create `services/data_quality/rules/contact_book_orphan.py`：

```python
"""rule: ContactBookEntry.student_id 指向不存在 student。"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.data_quality._base import Rule, Violation


class ContactBookOrphanRule(Rule):
    code = "contact_book_orphan_student"
    severity = "P0"
    description = "ContactBookEntry.student_id 指向不存在的 student（FK 漏 cascade）"

    def check(self, session: Session) -> list[Violation]:
        rows = session.execute(
            text(
                """
                SELECT cb.id, cb.student_id
                FROM contact_book_entries cb
                LEFT JOIN students s ON s.id = cb.student_id
                WHERE s.id IS NULL
                """
            )
        ).all()
        return [
            Violation(
                rule_code=self.code,
                severity=self.severity,
                entity_type="contact_book_entry",
                entity_id=str(row.id),
                summary=f"ContactBookEntry #{row.id} 指向不存在 student #{row.student_id}",
            )
            for row in rows
        ]
```

```bash
pytest tests/test_data_quality_rules.py -k "contact_book_orphan" -xvs
git add services/data_quality/rules/contact_book_orphan.py tests/test_data_quality_rules.py
git commit -m "feat(data-quality): contact_book_orphan_student rule (3/5)"
```

### 4c: guardian_orphan_user

- [ ] **Step 1: 寫 failing test**

```python
def test_guardian_orphan_user_detects(db_session_factory):
    from models.guardian import Guardian

    session = db_session_factory()
    # 同上：guardian.user_id 指向不存在 user
    g = Guardian(student_id=1, user_id=9999999, relation="father")
    session.add(g)
    session.commit()  # 若 FK 強制需先 SAVEPOINT 等繞

    from services.data_quality.rules.guardian_orphan_user import GuardianOrphanRule

    rule = GuardianOrphanRule()
    violations = rule.check(session)
    assert any(v.entity_id == str(g.id) for v in violations)
```

- [ ] **Step 2-4: 實作 + commit**

Create `services/data_quality/rules/guardian_orphan_user.py`：

```python
"""rule: Guardian.user_id 指向不存在 user。"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.data_quality._base import Rule, Violation


class GuardianOrphanRule(Rule):
    code = "guardian_orphan_user"
    severity = "P0"
    description = "Guardian.user_id 指向不存在的 user"

    def check(self, session: Session) -> list[Violation]:
        rows = session.execute(
            text(
                """
                SELECT g.id, g.user_id
                FROM guardians g
                LEFT JOIN users u ON u.id = g.user_id
                WHERE g.user_id IS NOT NULL AND u.id IS NULL
                """
            )
        ).all()
        return [
            Violation(
                rule_code=self.code,
                severity=self.severity,
                entity_type="guardian",
                entity_id=str(row.id),
                summary=f"Guardian #{row.id} 指向不存在 user #{row.user_id}",
            )
            for row in rows
        ]
```

```bash
pytest tests/test_data_quality_rules.py -k "guardian_orphan" -xvs
git add services/data_quality/rules/guardian_orphan_user.py tests/test_data_quality_rules.py
git commit -m "feat(data-quality): guardian_orphan_user rule (4/5)"
```

### 4d: salary_record_orphan_employee

- [ ] **Step 1-4: 寫 test + 實作 + commit**

```python
def test_salary_record_orphan_employee_detects(db_session_factory):
    # 同樣繞 FK 模擬：INSERT salary_records 指向不存在 employee
    from sqlalchemy import text

    session = db_session_factory()
    session.execute(
        text(
            "INSERT INTO salary_records (employee_id, salary_year, salary_month, gross_salary) "
            "VALUES (9999999, 2026, 5, 0)"
        )
    )
    session.commit()

    from services.data_quality.rules.salary_no_employee import SalaryOrphanRule

    rule = SalaryOrphanRule()
    violations = rule.check(session)
    assert any(v.rule_code == "salary_record_orphan_employee" for v in violations)
```

Create `services/data_quality/rules/salary_no_employee.py`：

```python
"""rule: SalaryRecord.employee_id 指向不存在 employee。"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.data_quality._base import Rule, Violation


class SalaryOrphanRule(Rule):
    code = "salary_record_orphan_employee"
    severity = "P0"
    description = "SalaryRecord.employee_id 指向不存在的 employee"

    def check(self, session: Session) -> list[Violation]:
        rows = session.execute(
            text(
                """
                SELECT sr.id, sr.employee_id, sr.salary_year, sr.salary_month
                FROM salary_records sr
                LEFT JOIN employees e ON e.id = sr.employee_id
                WHERE e.id IS NULL
                """
            )
        ).all()
        return [
            Violation(
                rule_code=self.code,
                severity=self.severity,
                entity_type="salary_record",
                entity_id=str(row.id),
                summary=f"SalaryRecord #{row.id} ({row.salary_year}-{row.salary_month}) 指向不存在 employee #{row.employee_id}",
            )
            for row in rows
        ]
```

```bash
pytest tests/test_data_quality_rules.py -k "salary_record_orphan" -xvs
git add services/data_quality/rules/salary_no_employee.py tests/test_data_quality_rules.py
git commit -m "feat(data-quality): salary_record_orphan_employee rule (5/5)"
```

---

## Task 5: Engine.run_all_rules orchestrator

**Files:**
- Create: `services/data_quality/engine.py`
- Test: `tests/test_data_quality_rules.py`

- [ ] **Step 1: 寫 failing test**

```python
def test_run_all_rules_returns_list_of_violations(db_session_factory):
    """run_all_rules 跑全部 5 rule，回傳合併 Violation list。"""
    from services.data_quality.engine import run_all_rules, ALL_RULES

    assert len(ALL_RULES) == 5
    rule_codes = {r.code for r in ALL_RULES}
    assert rule_codes == {
        "employee_active_but_offboarded",
        "student_active_but_lifecycle_terminal",
        "contact_book_orphan_student",
        "guardian_orphan_user",
        "salary_record_orphan_employee",
    }

    session = db_session_factory()
    violations = run_all_rules(session)
    assert isinstance(violations, list)
    # 預期回 list[Violation]（空也合理，看 fixture 設了什麼）
```

- [ ] **Step 2: Run failing test**

```bash
pytest tests/test_data_quality_rules.py -k "run_all_rules" -xvs
```
Expected: FAIL — import error

- [ ] **Step 3: 實作**

Create `services/data_quality/engine.py`：

```python
"""services/data_quality/engine.py — rule 跑批 orchestrator。"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from services.data_quality._base import Rule, Violation
from services.data_quality.rules.contact_book_orphan import ContactBookOrphanRule
from services.data_quality.rules.employee_offboard import EmployeeOffboardRule
from services.data_quality.rules.guardian_orphan_user import GuardianOrphanRule
from services.data_quality.rules.salary_no_employee import SalaryOrphanRule
from services.data_quality.rules.student_stale_active import StudentStaleActiveRule

logger = logging.getLogger(__name__)


ALL_RULES: list[Rule] = [
    EmployeeOffboardRule(),
    StudentStaleActiveRule(),
    ContactBookOrphanRule(),
    GuardianOrphanRule(),
    SalaryOrphanRule(),
]


def run_all_rules(session: Session) -> list[Violation]:
    """跑全部 ALL_RULES，回合併 list[Violation]。單條 rule 異常不阻斷其他。"""
    out: list[Violation] = []
    for rule in ALL_RULES:
        try:
            out.extend(rule.check(session))
        except Exception:
            logger.exception("data_quality rule %s failed", rule.code)
    return out
```

- [ ] **Step 4: Run pass + commit**

```bash
pytest tests/test_data_quality_rules.py -k "run_all_rules" -xvs
git add services/data_quality/engine.py tests/test_data_quality_rules.py
git commit -m "feat(data-quality): engine.run_all_rules orchestrator (5 rule swallow per-rule error)

Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch2.5"
```

---

## Task 6: Dispatch — persist + dedup

**Files:**
- Create: `services/data_quality/dispatch.py`
- Test: `tests/test_data_quality_dispatch.py`

- [ ] **Step 1: 寫 failing test**

Create `tests/test_data_quality_dispatch.py`：

```python
"""tests/test_data_quality_dispatch.py — Ch2 dispatch layer."""

from services.data_quality._base import Violation
from services.data_quality.dispatch import emit
from models.data_quality import DataQualityReport


def test_emit_writes_new_row_when_no_existing(db_session_factory):
    session = db_session_factory()
    v = Violation(
        rule_code="employee_active_but_offboarded",
        severity="P1",
        entity_type="employee",
        entity_id="42",
        summary="員工 #42 離職日已過...",
    )
    is_new = emit(v, session, line_queue=[])

    row = (
        session.query(DataQualityReport)
        .filter(DataQualityReport.dedup_key == v.dedup_key)
        .first()
    )
    assert row is not None
    assert row.status == "open"
    assert is_new is True


def test_emit_dedups_same_open_violation(db_session_factory):
    session = db_session_factory()
    v = Violation(
        rule_code="employee_active_but_offboarded",
        severity="P1",
        entity_type="employee",
        entity_id="42",
        summary="dup",
    )
    is_new_first = emit(v, session, line_queue=[])
    is_new_second = emit(v, session, line_queue=[])

    rows = (
        session.query(DataQualityReport)
        .filter(DataQualityReport.dedup_key == v.dedup_key)
        .all()
    )
    assert len(rows) == 1
    assert is_new_first is True
    assert is_new_second is False
    # last_seen_at 應有更新
    assert rows[0].last_seen_at >= rows[0].detected_at


def test_emit_skips_ignored_status(db_session_factory):
    session = db_session_factory()
    v = Violation(
        rule_code="x",
        severity="P2",
        entity_type="e",
        entity_id="1",
        summary="s",
    )
    # 預先插入 ignored row
    pre = DataQualityReport(
        rule_code=v.rule_code, severity=v.severity, entity_type=v.entity_type,
        entity_id=v.entity_id, summary="prev", dedup_key=v.dedup_key,
        status="ignored",
    )
    session.add(pre)
    session.commit()

    is_new = emit(v, session, line_queue=[])
    rows = (
        session.query(DataQualityReport)
        .filter(DataQualityReport.dedup_key == v.dedup_key)
        .all()
    )
    assert len(rows) == 1  # 不開新 row
    assert rows[0].status == "ignored"
    assert is_new is False
```

- [ ] **Step 2: Run failing test**

```bash
pytest tests/test_data_quality_dispatch.py -xvs
```
Expected: FAIL — import error

- [ ] **Step 3: 實作 dispatch.emit**

Create `services/data_quality/dispatch.py`：

```python
"""services/data_quality/dispatch.py — 4 線出口 (log + persist + sentry + line)。"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from models.data_quality import DataQualityReport
from services.data_quality._base import Violation
from utils.taipei_time import now_taipei_naive

logger = logging.getLogger("data_quality")


def emit(
    violation: Violation,
    session: Session,
    *,
    line_queue: list,
) -> bool:
    """寫一條 violation 進 4 線：log + persist + （新 open 時）Sentry + 累積到 line_queue。

    Returns: True 若這次是「新 open」（會 push Sentry + LINE）；False 表示同 dedup_key 已有
    open / ignored row，只更新 last_seen_at。
    """
    # 1. log
    logger.warning(
        "data_quality violation: rule=%s entity=%s/%s severity=%s",
        violation.rule_code,
        violation.entity_type,
        violation.entity_id,
        violation.severity,
    )

    # 2. persist + dedup
    existing: Optional[DataQualityReport] = (
        session.query(DataQualityReport)
        .filter(
            DataQualityReport.dedup_key == violation.dedup_key,
            DataQualityReport.status.in_(["open", "ignored"]),
        )
        .first()
    )
    if existing is not None:
        existing.last_seen_at = now_taipei_naive()
        session.commit()
        return False

    row = DataQualityReport(
        rule_code=violation.rule_code,
        severity=violation.severity,
        entity_type=violation.entity_type,
        entity_id=violation.entity_id,
        summary=violation.summary,
        dedup_key=violation.dedup_key,
        status="open",
    )
    session.add(row)
    session.commit()

    # 3+4: Sentry + LINE 累積（呼叫端 flush）
    _emit_sentry(violation)
    line_queue.append(violation)

    return True


def _emit_sentry(violation: Violation) -> None:
    """Sentry capture_message（warning level）。"""
    try:
        import sentry_sdk

        level_map = {"P0": "error", "P1": "warning", "P2": "info"}
        sentry_sdk.capture_message(
            f"data_quality: {violation.rule_code}",
            level=level_map.get(violation.severity, "warning"),
            tags={
                "rule_code": violation.rule_code,
                "entity_type": violation.entity_type,
                "severity": violation.severity,
            },
        )
    except Exception:
        logger.warning("sentry capture_message failed for %s", violation.rule_code)


def flush_line_digest(line_queue: list[Violation]) -> None:
    """把累積的 violation 合成一則 LINE flex bubble 推給老闆。空 queue 不推。"""
    if not line_queue:
        return
    try:
        from main import line_service
    except Exception:
        logger.warning("line_service unavailable, skip LINE digest")
        return

    head = line_queue[:3]
    rest = len(line_queue) - 3
    body_lines = [f"{v.severity} | {v.rule_code} | {v.summary}" for v in head]
    if rest > 0:
        body_lines.append(f"...另 {rest} 條請至後台 DataQuality 查看")

    text = "資料品質告警\n" + "\n".join(body_lines)
    try:
        line_service._push(text)
    except Exception:
        logger.exception("line_service push failed for data_quality digest")
```

- [ ] **Step 4: Run tests pass + commit**

```bash
pytest tests/test_data_quality_dispatch.py -xvs
git add services/data_quality/dispatch.py tests/test_data_quality_dispatch.py
git commit -m "feat(data-quality): dispatch.emit + flush_line_digest (4 線出口)

dedup: 同 dedup_key 已 open/ignored row 不重發 Sentry/LINE，只更新 last_seen_at。
Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch2.5"
```

---

## Task 7: Scheduler 注入 + config flag

**Files:**
- Create: `services/data_quality_scheduler.py`
- Modify: `config/scheduler.py`、`main.py`
- Test: `tests/test_data_quality_scheduler.py`

- [ ] **Step 1: 加 config flag**

Modify `config/scheduler.py`，在 `SchedulerSettings` 內加：

```python
    # Data quality (Ch2 of observability-forensic spec)
    data_quality_enabled: BoolEnv = False
    data_quality_check_interval: int = 60       # 每分鐘檢查是否到目標時間
    data_quality_hour: int = 3                  # 03:00 Asia/Taipei
    data_quality_minute: int = 0
```

- [ ] **Step 2: 寫 failing test**

Create `tests/test_data_quality_scheduler.py`：

```python
"""tests/test_data_quality_scheduler.py — Ch2 scheduler step。"""

from unittest.mock import patch


def test_scheduler_skips_when_disabled(monkeypatch):
    """data_quality_enabled=False → run_data_quality_once 不 emit。"""
    from services.data_quality_scheduler import scheduler_enabled

    monkeypatch.setenv("DATA_QUALITY_ENABLED", "false")
    # 重新讀 config（用 config.reset_for_tests 或新建 settings instance）
    from config import get_settings
    get_settings.cache_clear()

    assert scheduler_enabled() is False


def test_run_data_quality_once_calls_engine_and_dispatch():
    """run_data_quality_once 應呼叫 engine.run_all_rules + dispatch.emit + flush_line_digest。"""
    from services.data_quality_scheduler import run_data_quality_once
    from services.data_quality._base import Violation

    fake_v = Violation(
        rule_code="x", severity="P0", entity_type="e", entity_id="1", summary="s"
    )

    with patch(
        "services.data_quality_scheduler.run_all_rules", return_value=[fake_v]
    ) as m_run, patch(
        "services.data_quality_scheduler.emit", return_value=True
    ) as m_emit, patch(
        "services.data_quality_scheduler.flush_line_digest"
    ) as m_flush:
        result = run_data_quality_once()

    assert m_run.called
    assert m_emit.called
    assert m_flush.called
    assert result["detected"] == 1
```

- [ ] **Step 3: Run failing test**

```bash
pytest tests/test_data_quality_scheduler.py -xvs
```
Expected: FAIL

- [ ] **Step 4: 實作 scheduler**

Create `services/data_quality_scheduler.py`：

```python
"""services/data_quality_scheduler.py — 每日 03:00 跑 data quality rules。

沿用 finance_reconciliation_scheduler.py 的 pattern：asyncio loop + opt-in env var
+ try_scheduler_lock 防多 worker。
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from config import get_settings
from models.database import get_session
from services.data_quality.dispatch import emit, flush_line_digest
from services.data_quality.engine import run_all_rules
from utils.scheduler_observability import record_rows, scheduler_iteration

logger = logging.getLogger(__name__)
TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.data_quality_enabled)


def _target_hm() -> tuple[int, int]:
    s = get_settings().scheduler
    return (s.data_quality_hour, s.data_quality_minute)


def run_data_quality_once() -> dict:
    """同步執行一輪 rule + dispatch；可手動觸發。回傳統計摘要。"""
    line_queue = []
    session = get_session()
    try:
        violations = run_all_rules(session)
        new_open = 0
        for v in violations:
            if emit(v, session, line_queue=line_queue):
                new_open += 1
        flush_line_digest(line_queue)
        return {
            "detected": len(violations),
            "new_open": new_open,
            "ran_at": datetime.now(TAIPEI_TZ).isoformat(),
        }
    finally:
        session.close()


async def data_quality_scheduler_loop(stop_event: asyncio.Event) -> None:
    """每分鐘檢查當下時間是否到 target_hm；到了拿 advisory lock 跑一次。"""
    from utils.advisory_lock import try_scheduler_lock

    last_run_date = None
    while not stop_event.is_set():
        if not scheduler_enabled():
            await asyncio.sleep(get_settings().scheduler.data_quality_check_interval)
            continue

        now = datetime.now(TAIPEI_TZ)
        target_hour, target_minute = _target_hm()
        if (
            now.hour == target_hour
            and now.minute >= target_minute
            and last_run_date != now.date()
        ):
            try:
                with try_scheduler_lock("data_quality_daily") as got_lock:
                    if got_lock:
                        with scheduler_iteration("data_quality"):
                            result = run_data_quality_once()
                            record_rows("data_quality", result.get("detected", 0))
                        last_run_date = now.date()
                        logger.info("data_quality scheduler done: %s", result)
            except Exception:
                logger.exception("data_quality scheduler iteration failed")

        await asyncio.sleep(get_settings().scheduler.data_quality_check_interval)


def start_data_quality_scheduler(loop: asyncio.AbstractEventLoop) -> asyncio.Event:
    """main.py startup 呼叫；回 stop_event 給 shutdown 用。"""
    stop_event = asyncio.Event()
    loop.create_task(data_quality_scheduler_loop(stop_event))
    return stop_event
```

- [ ] **Step 5: Run tests pass**

```bash
pytest tests/test_data_quality_scheduler.py -xvs
```
Expected: PASS

- [ ] **Step 6: 在 main.py 注入 scheduler**

Find 既有 `start_finance_reconciliation_scheduler` 或類似 scheduler 啟動處（grep `start_.*_scheduler` 在 main.py），加入：

```python
from services.data_quality_scheduler import start_data_quality_scheduler

# 在既有 scheduler 注入序列之後加
_data_quality_stop_event = start_data_quality_scheduler(loop)

# shutdown handler 加
_data_quality_stop_event.set()
```

- [ ] **Step 7: Commit**

```bash
git add services/data_quality_scheduler.py config/scheduler.py main.py tests/test_data_quality_scheduler.py
git commit -m "feat(data-quality): scheduler 注入 (03:00 Taipei, opt-in via DATA_QUALITY_ENABLED)

run_data_quality_once 提供手動觸發入口，loop 用 advisory_lock 防多 worker 重複跑。
預設 enabled=False — HR 確認 baseline 不雜音再開。
Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch2.4"
```

---

## Task 8: API endpoints — list / ack / resolve / ignore / run-now

**Files:**
- Create: `api/data_quality.py`
- Modify: `main.py`（註冊 router）
- Test: `tests/test_data_quality_endpoints.py`

- [ ] **Step 1: 寫 failing test (list endpoint)**

Create `tests/test_data_quality_endpoints.py`：

```python
"""tests/test_data_quality_endpoints.py — Ch2 API endpoints."""

import pytest

from models.data_quality import DataQualityReport
from utils.permissions import Permission


def test_list_reports_requires_permission(client, anon_token):
    resp = client.get(
        "/api/data-quality/reports",
        headers={"Authorization": f"Bearer {anon_token}"},
    )
    assert resp.status_code in (401, 403)


def test_list_reports_filters_by_status(client, admin_token, db_session_factory):
    session = db_session_factory()
    session.add(DataQualityReport(
        rule_code="x", severity="P0", entity_type="e", entity_id="1",
        summary="s", dedup_key="d1", status="open",
    ))
    session.add(DataQualityReport(
        rule_code="x", severity="P0", entity_type="e", entity_id="2",
        summary="s", dedup_key="d2", status="fixed",
    ))
    session.commit()

    resp = client.get(
        "/api/data-quality/reports?status=open",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert all(r["status"] == "open" for r in data["items"])


def test_ack_marks_status(client, admin_token, db_session_factory):
    session = db_session_factory()
    row = DataQualityReport(
        rule_code="x", severity="P0", entity_type="e", entity_id="3",
        summary="s", dedup_key="d3", status="open",
    )
    session.add(row)
    session.commit()
    rid = row.id

    resp = client.post(
        f"/api/data-quality/reports/{rid}/ack",
        json={"note": "看到了"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    session.refresh(row)
    assert row.status == "ack"
    assert row.ack_at is not None


def test_resolve_marks_status_and_note(client, admin_token, db_session_factory):
    session = db_session_factory()
    row = DataQualityReport(
        rule_code="x", severity="P0", entity_type="e", entity_id="4",
        summary="s", dedup_key="d4", status="open",
    )
    session.add(row)
    session.commit()

    resp = client.post(
        f"/api/data-quality/reports/{row.id}/resolve",
        json={"note": "已修正：手動關閉 is_active"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    session.refresh(row)
    assert row.status == "fixed"
    assert "手動關閉" in row.resolution_note


def test_run_now_returns_summary(client, admin_token):
    resp = client.post(
        "/api/data-quality/run-now",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "detected" in body
```

**Fixture 提示**：`client`、`admin_token`、`anon_token` 通常在 `tests/conftest.py` 有定義；若無，先 grep 既有 endpoint test 找命名。

- [ ] **Step 2: Run failing test**

```bash
pytest tests/test_data_quality_endpoints.py -xvs
```
Expected: 404 全 fail

- [ ] **Step 3: 實作 router**

Create `api/data_quality.py`：

```python
"""api/data_quality.py — Ch2 data quality 後台管理。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.base import get_session_dep
from models.data_quality import DataQualityReport
from services.data_quality_scheduler import run_data_quality_once
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.taipei_time import now_taipei_naive

# 沿用既有共用分頁 helper（PR #43 已 ship）
try:
    from utils.pagination import paginate_query
except ImportError:
    paginate_query = None  # 若 helper 尚未 ship 此 codebase，下面 fallback


router = APIRouter(prefix="/api/data-quality", tags=["data-quality"])


class ReportOut(BaseModel):
    id: int
    rule_code: str
    severity: str
    entity_type: str
    entity_id: str
    summary: str
    status: str
    detected_at: datetime
    last_seen_at: datetime
    ack_at: Optional[datetime]
    resolved_at: Optional[datetime]
    resolution_note: Optional[str]

    class Config:
        from_attributes = True


class ListReportsOut(BaseModel):
    items: list[ReportOut]
    total: int
    page: int
    page_size: int


class AckBody(BaseModel):
    note: Optional[str] = None


class ResolveBody(BaseModel):
    note: str


class IgnoreBody(BaseModel):
    note: str


class RunNowOut(BaseModel):
    detected: int
    new_open: int
    ran_at: str


@router.get("/reports", response_model=ListReportsOut)
def list_reports(
    status: Optional[str] = Query(None),
    rule_code: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session_dep),
    _: object = Depends(require_staff_permission(Permission.DATA_QUALITY_READ)),
):
    q = session.query(DataQualityReport)
    if status:
        q = q.filter(DataQualityReport.status == status)
    if rule_code:
        q = q.filter(DataQualityReport.rule_code == rule_code)
    if severity:
        q = q.filter(DataQualityReport.severity == severity)
    q = q.order_by(DataQualityReport.detected_at.desc())

    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return ListReportsOut(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


def _get_report_or_404(session: Session, report_id: int) -> DataQualityReport:
    row = session.query(DataQualityReport).filter(DataQualityReport.id == report_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    return row


@router.post("/reports/{report_id}/ack")
def ack_report(
    report_id: int,
    body: AckBody,
    session: Session = Depends(get_session_dep),
    current_user=Depends(require_staff_permission(Permission.DATA_QUALITY_WRITE)),
):
    row = _get_report_or_404(session, report_id)
    if row.status != "open":
        raise HTTPException(status_code=400, detail=f"cannot ack from status={row.status}")
    row.status = "ack"
    row.ack_at = now_taipei_naive()
    row.ack_by = current_user.id
    if body.note:
        row.resolution_note = body.note
    session.commit()
    return {"ok": True, "id": row.id, "status": row.status}


@router.post("/reports/{report_id}/resolve")
def resolve_report(
    report_id: int,
    body: ResolveBody,
    session: Session = Depends(get_session_dep),
    current_user=Depends(require_staff_permission(Permission.DATA_QUALITY_WRITE)),
):
    row = _get_report_or_404(session, report_id)
    if row.status == "fixed":
        return {"ok": True, "id": row.id, "status": row.status}  # idempotent
    row.status = "fixed"
    row.resolved_at = now_taipei_naive()
    row.resolution_note = body.note
    if not row.ack_at:
        row.ack_at = row.resolved_at
        row.ack_by = current_user.id
    session.commit()
    return {"ok": True, "id": row.id, "status": row.status}


@router.post("/reports/{report_id}/ignore")
def ignore_report(
    report_id: int,
    body: IgnoreBody,
    session: Session = Depends(get_session_dep),
    current_user=Depends(require_staff_permission(Permission.DATA_QUALITY_WRITE)),
):
    row = _get_report_or_404(session, report_id)
    row.status = "ignored"
    row.ack_at = now_taipei_naive()
    row.ack_by = current_user.id
    row.resolution_note = body.note
    session.commit()
    return {"ok": True, "id": row.id, "status": row.status}


@router.post("/run-now", response_model=RunNowOut)
def run_now(
    _: object = Depends(require_staff_permission(Permission.DATA_QUALITY_WRITE)),
):
    return RunNowOut(**run_data_quality_once())
```

- [ ] **Step 4: 註冊 router 到 main.py**

Modify `main.py` 找既有 `app.include_router(...)` 段，加：

```python
from api import data_quality as data_quality_router

app.include_router(data_quality_router.router)
```

- [ ] **Step 5: Run tests pass**

```bash
pytest tests/test_data_quality_endpoints.py -xvs
```
Expected: 5 PASS

- [ ] **Step 6: Commit**

```bash
git add api/data_quality.py main.py tests/test_data_quality_endpoints.py
git commit -m "feat(data-quality): API endpoints (list/ack/resolve/ignore/run-now)

Permission gate: DATA_QUALITY_READ 看 / WRITE 操作。沿用 PR#43 分頁 helper（若已 ship）。
Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch2.6"
```

---

## Task 9: Backend 整體 regression + commit

**Files:**（純驗證）

- [ ] **Step 1: 跑 data_quality 全 suite**

```bash
pytest tests/test_data_quality_*.py -xvs
```
Expected: 全 PASS（預期約 17-20 個 test：rules 5×2=10 + dispatch 3 + scheduler 2 + endpoint 5）

- [ ] **Step 2: 全套 pytest 零 regression**

```bash
pytest tests/ --tb=short 2>&1 | tail -40
```
Expected: 既有 pass 數不減；新增 test 全綠。

- [ ] **Step 3: 若有 regression 修，commit**

```bash
git add -A
git commit -m "fix(data-quality): backend regression 修正"
```

---

## Task 10: 前端 OpenAPI codegen

**Files:**
- Modify: `src/api/_generated/schema.d.ts`（regen）

**前置**：本 task 在 ivy-frontend repo（跨 repo），需 cd 到 frontend worktree 或 `/Users/yilunwu/Desktop/ivy-frontend`。

- [ ] **Step 1: 後端 dump openapi**

在 ivy-backend repo（worktree）：

```bash
python scripts/dump_openapi.py
```
Expected: 產 `openapi.json`（local-only，.gitignore 擋）。

- [ ] **Step 2: 前端 regen schema**

cd 到 ivy-frontend：

```bash
cd ~/Desktop/ivy-frontend
npm run gen:api
```
Expected: `src/api/_generated/schema.d.ts` 更新，含 `/data-quality/reports` / `/run-now` 等 paths。

- [ ] **Step 3: 確認新 path 出現**

```bash
grep -A 3 "/data-quality/reports" src/api/_generated/schema.d.ts | head -20
```
Expected: 看到 list / ack / resolve / ignore / run-now 5 path schema。

- [ ] **Step 4: Commit**

```bash
git add src/api/_generated/schema.d.ts
git commit -m "chore(api): regen schema.d.ts 含 /data-quality endpoints

Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch2"
```

---

## Task 11: 前端 src/api/dataQuality.ts

**Files:**
- Create: `src/api/dataQuality.ts`
- Test: 暫無（pure axios wrapper，type 已由 schema.d.ts 控）

- [ ] **Step 1: 建 api wrapper**

Create `src/api/dataQuality.ts`：

```typescript
import api from './index'
import type { ApiBody, ApiQuery, AxiosResp, ApiResponse } from './_generated/typed'

type Reports = ApiResponse<'/data-quality/reports', 'get'>
type AckBody = ApiBody<'/data-quality/reports/{report_id}/ack', 'post'>
type ResolveBody = ApiBody<'/data-quality/reports/{report_id}/resolve', 'post'>
type IgnoreBody = ApiBody<'/data-quality/reports/{report_id}/ignore', 'post'>
type RunNowResp = ApiResponse<'/data-quality/run-now', 'post'>

export function listReports(
  query: ApiQuery<'/data-quality/reports', 'get'>,
): AxiosResp<Reports> {
  return api.get('/data-quality/reports', { params: query })
}

export function ackReport(id: number, body: AckBody): AxiosResp<unknown> {
  return api.post(`/data-quality/reports/${id}/ack`, body)
}

export function resolveReport(id: number, body: ResolveBody): AxiosResp<unknown> {
  return api.post(`/data-quality/reports/${id}/resolve`, body)
}

export function ignoreReport(id: number, body: IgnoreBody): AxiosResp<unknown> {
  return api.post(`/data-quality/reports/${id}/ignore`, body)
}

export function runNow(): AxiosResp<RunNowResp> {
  return api.post('/data-quality/run-now')
}
```

- [ ] **Step 2: Type check**

```bash
npm run typecheck
```
Expected: 0 error。若有 `unknown` warning（後端 endpoint 缺 response_model），可暫過渡。

- [ ] **Step 3: Commit**

```bash
git add src/api/dataQuality.ts
git commit -m "feat(api): 加 src/api/dataQuality.ts wrapper (5 函式)

Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch2.7"
```

---

## Task 12: 前端 DataQualityView.vue + 主選單

**Files:**
- Create: `src/views/DataQualityView.vue`
- Modify: `src/router/index.ts`、主選單檔（grep `MainMenu\|AppSidebar\|menuItems`）
- Modify: `src/utils/permissions.ts`（PERMISSION_LABELS）
- Test: `src/views/__tests__/DataQualityView.test.ts`

- [ ] **Step 1: 寫 failing vitest**

Create `src/views/__tests__/DataQualityView.test.ts`：

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'

import DataQualityView from '../DataQualityView.vue'

vi.mock('@/api/dataQuality', () => ({
  listReports: vi.fn().mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 20 } }),
  ackReport: vi.fn().mockResolvedValue({ data: { ok: true } }),
  resolveReport: vi.fn().mockResolvedValue({ data: { ok: true } }),
  ignoreReport: vi.fn().mockResolvedValue({ data: { ok: true } }),
  runNow: vi.fn().mockResolvedValue({ data: { detected: 0, new_open: 0, ran_at: '2026-05-28T03:00:00+08:00' } }),
}))

describe('DataQualityView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders empty state when no reports', async () => {
    const wrapper = mount(DataQualityView, {
      global: { plugins: [ElementPlus] },
    })
    await new Promise(r => setTimeout(r, 0))
    expect(wrapper.text()).toContain('資料品質報告')
  })

  it('calls listReports on mount', async () => {
    const { listReports } = await import('@/api/dataQuality')
    mount(DataQualityView, {
      global: { plugins: [ElementPlus] },
    })
    await new Promise(r => setTimeout(r, 0))
    expect(listReports).toHaveBeenCalled()
  })

  it('filter status triggers reload', async () => {
    const { listReports } = await import('@/api/dataQuality')
    const wrapper = mount(DataQualityView, {
      global: { plugins: [ElementPlus] },
    })
    await new Promise(r => setTimeout(r, 0))
    vi.mocked(listReports).mockClear()

    // 假設 view 有 status filter select；觸發 change
    await wrapper.find('[data-testid="status-filter"]').trigger('change')
    expect(listReports).toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run failing test**

```bash
npm run test -- DataQualityView
```
Expected: FAIL

- [ ] **Step 3: 實作 view**

Create `src/views/DataQualityView.vue`：

```vue
<template>
  <div class="data-quality-view">
    <header class="header">
      <h2>資料品質報告</h2>
      <div class="counters">
        <el-tag type="danger">P0: {{ counts.P0 }}</el-tag>
        <el-tag type="warning">P1: {{ counts.P1 }}</el-tag>
        <el-tag type="info">P2: {{ counts.P2 }}</el-tag>
      </div>
      <el-button
        v-if="canWrite"
        type="primary"
        :loading="running"
        @click="onRunNow"
      >
        立即執行
      </el-button>
    </header>

    <div class="filters">
      <el-select
        v-model="filters.status"
        data-testid="status-filter"
        placeholder="狀態"
        clearable
        @change="reload"
      >
        <el-option label="開啟" value="open" />
        <el-option label="已確認" value="ack" />
        <el-option label="已修正" value="fixed" />
        <el-option label="忽略" value="ignored" />
      </el-select>

      <el-select
        v-model="filters.severity"
        placeholder="嚴重度"
        clearable
        @change="reload"
      >
        <el-option label="P0" value="P0" />
        <el-option label="P1" value="P1" />
        <el-option label="P2" value="P2" />
      </el-select>
    </div>

    <el-table :data="rows" v-loading="loading">
      <el-table-column label="時間" prop="detected_at" width="170" />
      <el-table-column label="嚴重度" prop="severity" width="80" />
      <el-table-column label="規則" prop="rule_code" />
      <el-table-column label="實體" width="150">
        <template #default="{ row }">
          {{ row.entity_type }} #{{ row.entity_id }}
        </template>
      </el-table-column>
      <el-table-column label="摘要" prop="summary" />
      <el-table-column label="狀態" prop="status" width="100" />
      <el-table-column label="操作" width="220" v-if="canWrite">
        <template #default="{ row }">
          <el-button v-if="row.status === 'open'" size="small" @click="onAck(row)">確認</el-button>
          <el-button v-if="row.status !== 'fixed'" size="small" type="success" @click="onResolve(row)">修正</el-button>
          <el-button v-if="row.status === 'open'" size="small" type="info" @click="onIgnore(row)">忽略</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="filters.page"
      v-model:page-size="filters.page_size"
      :total="total"
      :page-sizes="[20, 50, 100]"
      @current-change="reload"
      @size-change="reload"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  listReports as apiList,
  ackReport,
  resolveReport,
  ignoreReport,
  runNow,
} from '@/api/dataQuality'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const canWrite = computed(() => auth.hasPermission('DATA_QUALITY_WRITE'))

const filters = reactive({
  status: 'open',
  severity: '' as '' | 'P0' | 'P1' | 'P2',
  rule_code: '',
  page: 1,
  page_size: 20,
})

const rows = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const running = ref(false)

const counts = computed(() => {
  const c = { P0: 0, P1: 0, P2: 0 } as Record<string, number>
  rows.value.forEach(r => {
    if (r.status === 'open' && c[r.severity] !== undefined) c[r.severity]++
  })
  return c
})

async function reload() {
  loading.value = true
  try {
    const { data } = await apiList(filters as any)
    rows.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

async function onRunNow() {
  running.value = true
  try {
    const { data } = await runNow()
    ElMessage.success(`已執行：偵測 ${data.detected} 條，新開 ${data.new_open} 條`)
    await reload()
  } finally {
    running.value = false
  }
}

async function _promptNote(title: string): Promise<string | null> {
  try {
    const { value } = await ElMessageBox.prompt(title, '備註', {
      confirmButtonText: '確定',
      cancelButtonText: '取消',
    })
    return value
  } catch {
    return null
  }
}

async function onAck(row: any) {
  const note = await _promptNote('確認此條違規')
  if (note === null) return
  await ackReport(row.id, { note })
  await reload()
}

async function onResolve(row: any) {
  const note = await _promptNote('修正說明（必填）')
  if (!note) return
  await resolveReport(row.id, { note })
  await reload()
}

async function onIgnore(row: any) {
  const note = await _promptNote('忽略原因（必填）')
  if (!note) return
  await ignoreReport(row.id, { note })
  await reload()
}

onMounted(reload)
</script>

<style scoped>
.data-quality-view {
  padding: 16px;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.counters {
  display: flex;
  gap: 8px;
}
.filters {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
</style>
```

- [ ] **Step 4: 加 router**

Modify `src/router/index.ts`，在既有 admin route 區段加：

```typescript
{
  path: '/data-quality',
  name: 'DataQuality',
  component: () => import('@/views/DataQualityView.vue'),
  meta: { requiresAuth: true, permission: 'DATA_QUALITY_READ' },
},
```

- [ ] **Step 5: 加主選單**

找到主選單檔（grep `el-menu-item\|menuItems` 在 `src/components/layout/` 或 `src/components/SidebarMenu*`），加：

```typescript
{ title: '資料品質', icon: 'WarningFilled', route: '/data-quality', permission: 'DATA_QUALITY_READ' },
```

- [ ] **Step 6: PERMISSION_LABELS 同步**

Modify `src/utils/permissions.ts`（或對應檔），加：

```typescript
DATA_QUALITY_READ: '資料品質報告 — 檢視',
DATA_QUALITY_WRITE: '資料品質報告 — 處理',
```

- [ ] **Step 7: Run vitest pass**

```bash
npm run test -- DataQualityView
```
Expected: 3 PASS

- [ ] **Step 8: Run typecheck + build**

```bash
npm run typecheck && npm run build
```
Expected: 0 error / build OK

- [ ] **Step 9: Commit**

```bash
git add src/views/DataQualityView.vue src/views/__tests__/DataQualityView.test.ts src/router/index.ts src/utils/permissions.ts src/components/layout/...
git commit -m "feat(ui): DataQualityView 後台管理頁面 + 路由 + 主選單

filter (status/severity) + counter chip + 5 endpoint 串接 + permission gate。
3 vitest 覆蓋 render / mount-call / filter-trigger-reload。
Refs: 2026-05-28-observability-forensic-and-design-tokens-design.md Ch2.7"
```

---

## Task 13: 全套 vitest + 整合驗證

- [ ] **Step 1: vitest 全套**

```bash
cd ~/Desktop/ivy-frontend
npm run test
```
Expected: 既有 + 新增 3 都 PASS，0 regression。

- [ ] **Step 2: backend + frontend dev server 起來實點一次**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh
```

瀏覽 http://localhost:5173 → 主選單「資料品質」→ 應看到空 table + 立即執行 button → 按 button → 應觸發 backend POST /run-now → backend log 看到 `data_quality scheduler done: {...}`。

- [ ] **Step 3: 確認 0 commit 漏**

```bash
cd ~/Desktop/ivy-backend && git log spec/observability-forensic-tokens-2026-05-28-backend --oneline | head -20
cd ~/Desktop/ivy-frontend && git log <feature-branch> --oneline | head -10
```

---

## Self-Review Checklist

- [x] **Spec coverage**：Ch2 全 9 section 覆蓋
  - 2.1 schema → Task 1
  - 2.2 模組結構 → Task 3-6
  - 2.3 5 rule → Task 3-4
  - 2.4 scheduler → Task 7
  - 2.5 4 線 dispatch → Task 6
  - 2.6 permission/endpoint → Task 2 + 8
  - 2.7 前端 view → Task 10-12
  - 2.8 測試 → 散在各 task
  - 2.9 CLAUDE.md 對齊 → Task 1 step 5 + Task 4 各 rule summary 不放 PII
- [x] **Placeholder scan**：無 TBD/TODO；migration head 為「step 1 抓到」明確指示
- [x] **Type consistency**：`Violation` NamedTuple 與 `emit()` 簽章一致；`run_data_quality_once` return dict 含 `detected`/`new_open`/`ran_at` 三 key，scheduler test + endpoint test 都用此 key
- [x] **PII 控管**：所有 rule summary 用 id 不用 name；dispatch.flush_line_digest 拼字串用 summary（已控）
- [x] **單一 alembic head**：dqreport01 → auditfor01 → ... 鏈接

## 風險與緩解（plan 層）

| 風險 | 緩解 |
|---|---|
| FK 強制下測試無法 INSERT 孤兒 row | 用「先 INSERT 父表 → 取 id → 刪父表」繞；或測試 conftest 提供 `SET CONSTRAINTS ALL DEFERRED` fixture |
| `try_scheduler_lock` API name 不同（advisory_lock）| Task 7 step 4 已 import 對齊；若名稱不符 grep 既有 scheduler 看正確名 |
| Pagination helper PR #43 未 merge | Task 8 用直接 .offset/.limit fallback，commit message 標註 follow-up 可切到 helper |
| frontend admin 路徑可能用 `/admin/data-quality` 而非 `/data-quality` | Task 12 step 4 grep 既有 admin route pattern 對齊 |
| ElementPlus 主選單 menu item key 結構不同 codebase 有自己慣例 | Task 12 step 5 explicit grep `MainMenu\|AppSidebar\|menuItems` 找正解 |
| LineService 在 dev 環境無 token → digest fail | dispatch.flush_line_digest 已 try/except 包 + logger.warning，不阻 scheduler |
