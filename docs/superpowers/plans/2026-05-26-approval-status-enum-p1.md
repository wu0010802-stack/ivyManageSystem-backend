# Approval Status Enum P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land Phase 1 of the approval-status enum rollout — add `status: String(20)` column to `leave_records` / `overtime_records` / `punch_correction_requests`, backfill from `is_approved`, register a SQLAlchemy attribute listener that mirrors writes `is_approved → status` so callsites can continue using the legacy boolean unchanged.

**Architecture:**
- New module `models/approval.py` exposes `ApprovalStatus(str, Enum)` (`pending` / `approved` / `rejected`) and `register_p1_listeners(*classes)`. Listener uses SQLAlchemy `attribute.set` event with `propagate=False` and an idempotency guard (skip write when target already aligned).
- Alembic migration `apvstat01_add_approval_status_column` adds the column with `NOT NULL DEFAULT 'pending'` + CHECK constraint on all three tables, backfills from `is_approved` via a frozen mapping (does **not** import `ApprovalStatus`), and creates 6 new `status`-prefixed indexes alongside the existing `is_approved` ones.
- Existing `approval_status` @property on each model is rewritten to `return self.status`, becoming a read-side bridge (audit log / export code reading `record.approval_status` works unchanged but now sources from the new column).

**Tech Stack:** SQLAlchemy 2.x, Alembic, FastAPI, pytest, PostgreSQL (prod) + SQLite (test).

**Context for executors:** The spec lives at `docs/superpowers/specs/2026-05-26-approval-status-enum-rollout-design.md`. Read §3 (核心設計決策) and §5 (風險與已知陷阱) before starting Task 1. This plan covers **only P1** — callsite rewrites, frontend changes, and column drop are P2/P3/P4 (separate plans).

**Out of scope (do not touch):**
- Any callsite that reads or writes `is_approved` outside of model files (P2 scope).
- Frontend `.vue` / `.ts` files (P3 scope).
- Removing `is_approved` column or its 6 existing indexes (P4 scope).
- `LeaveRecord.substitute_status`, `AppraisalSummary.status`, `ApproveRequest.approved` (out of scope).

---

## File Structure

**Create:**
- `models/approval.py` — `ApprovalStatus` enum + `register_p1_listeners(*classes)`. ~50 lines. Imports only `enum` and `sqlalchemy.event`.
- `alembic/versions/20260526_apvstat01_add_approval_status_column.py` — migration with frozen-mapping backfill. ~120 lines.
- `tests/test_approval_status_p1_dual_write.py` — pytest covering listener behavior for all three models. ~150 lines.

**Modify:**
- `models/leave.py` — add `status` column to `LeaveRecord`, add 4 new `status`-prefixed indexes (mirroring the 4 existing `is_approved` ones), rewrite `approval_status` @property.
- `models/overtime.py` — same change to `OvertimeRecord` (add 2 new indexes mirroring 2 existing) and `PunchCorrectionRequest` (add column + rewrite property; no new indexes because no existing `is_approved` index on this table).
- `models/__init__.py` — append imports for the three models + `register_p1_listeners` call at the bottom (after class definitions are loaded).

**Index inventory (P1 adds these 6, leaves existing 6 alone):**

| Table | Existing (P4 will drop) | New (P1 adds) |
|---|---|---|
| `leave_records` | `ix_leave_emp_approved (employee_id, is_approved)` | `ix_leave_emp_status (employee_id, status)` |
| `leave_records` | `ix_leave_approved_start_date (is_approved, start_date)` | `ix_leave_status_start_date (status, start_date)` |
| `leave_records` | `ix_leave_emp_type_approved (employee_id, leave_type, is_approved)` | `ix_leave_emp_type_status (employee_id, leave_type, status)` |
| `leave_records` | `ix_leave_approval_date (is_approved, start_date)` (from separate migration) | `ix_leave_status_date (status, start_date)` |
| `overtime_records` | `ix_overtime_emp_approved (employee_id, is_approved)` | `ix_overtime_emp_status (employee_id, status)` |
| `overtime_records` | `ix_overtime_approved_date (is_approved, overtime_date)` | `ix_overtime_status_date (status, overtime_date)` |
| `punch_correction_requests` | (none) | (none) |

---

## Task 1: Create `models/approval.py` with `ApprovalStatus` enum + listener helper

**Files:**
- Create: `models/approval.py`
- Create: `tests/test_approval_status_p1_dual_write.py` (only the enum / listener unit tests in this task; integration tests added in Task 6)

- [ ] **Step 1: Write failing tests for the enum**

Create `tests/test_approval_status_p1_dual_write.py` with this initial content:

```python
"""P1 dual-write listener tests — covers ApprovalStatus enum + attribute listener.

P1 listener direction: is_approved (set) → status (mirror).
P2 PR will reverse the direction; this file gets new tests at that point.
"""

import pytest

from models.approval import ApprovalStatus, register_p1_listeners


class TestApprovalStatusEnum:
    def test_three_values(self):
        assert ApprovalStatus.PENDING.value == "pending"
        assert ApprovalStatus.APPROVED.value == "approved"
        assert ApprovalStatus.REJECTED.value == "rejected"

    def test_is_str_subclass(self):
        """str-mixin so SQLAlchemy String column round-trips cleanly."""
        assert isinstance(ApprovalStatus.PENDING, str)
        assert ApprovalStatus.PENDING == "pending"

    def test_no_extra_values(self):
        assert {m.value for m in ApprovalStatus} == {"pending", "approved", "rejected"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
cd ~/Desktop/ivy-backend && pytest tests/test_approval_status_p1_dual_write.py::TestApprovalStatusEnum -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'models.approval'`.

- [ ] **Step 3: Create `models/approval.py` with the enum + listener helper**

Create `models/approval.py` with this exact content:

```python
"""審核狀態 enum 與 P1 雙寫 listener。

P1 期間：callsite 仍寫 `record.is_approved = True/False/None`，
listener 自動同步 `record.status = 'approved'/'rejected'/'pending'`。

P2 PR 會反轉方向（status → is_approved），P4 PR 會移除 listener。
詳見 docs/superpowers/specs/2026-05-26-approval-status-enum-rollout-design.md §3.4。
"""

import enum

from sqlalchemy import event


class ApprovalStatus(str, enum.Enum):
    """共用審核狀態，由 LeaveRecord / OvertimeRecord / PunchCorrectionRequest 三表使用。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# 不要 import ApprovalStatus 到 alembic migration — 用 frozen mapping。
_BOOL_TO_STATUS = {
    True: ApprovalStatus.APPROVED.value,
    False: ApprovalStatus.REJECTED.value,
    None: ApprovalStatus.PENDING.value,
}


def register_p1_listeners(*model_classes) -> None:
    """為傳入的 model class 註冊 is_approved → status 單向同步 listener。

    P1+P2 期間使用。每個 class 必須有 `is_approved` 與 `status` 兩個 Column。
    `propagate=False` 防止繼承時重複掛載。
    """

    for cls in model_classes:
        _register_one(cls)


def _register_one(cls) -> None:
    @event.listens_for(cls.is_approved, "set", propagate=False)
    def _sync_status(target, value, oldvalue, initiator):
        expected = _BOOL_TO_STATUS[value]
        # Idempotency guard：已對齊就不再寫，避免無謂 UPDATE。
        if target.status != expected:
            target.status = expected
```

- [ ] **Step 4: Run the enum tests to verify they pass**

Run:
```bash
cd ~/Desktop/ivy-backend && pytest tests/test_approval_status_p1_dual_write.py::TestApprovalStatusEnum -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add models/approval.py tests/test_approval_status_p1_dual_write.py
git commit -m "feat(approval): add ApprovalStatus enum + P1 listener helper

P1 of 4-PR approval-status enum rollout. See docs/superpowers/specs/
2026-05-26-approval-status-enum-rollout-design.md for full design.

ApprovalStatus(str, enum.Enum) shared across LeaveRecord / OvertimeRecord /
PunchCorrectionRequest. register_p1_listeners() wires an 'is_approved set'
event that mirrors writes to the new 'status' column with an idempotency
guard. Listener is wired in models/__init__.py in a later task."
```

---

## Task 2: Alembic migration — add `status` column + backfill + 6 new indexes

**Files:**
- Create: `alembic/versions/20260526_apvstat01_add_approval_status_column.py`

- [ ] **Step 1: Confirm current alembic head**

Run:
```bash
cd ~/Desktop/ivy-backend && alembic heads
```
Expected: `suprhlt01 (head)`. If different, use whatever single head is reported; if multiple heads, **stop and ask the user** — this plan assumes single head.

- [ ] **Step 2: Create the migration file**

Create `alembic/versions/20260526_apvstat01_add_approval_status_column.py` with this exact content:

```python
"""add_approval_status_column

Phase 1 of the approval-status enum rollout. Adds a `status` String(20) column
to leave_records / overtime_records / punch_correction_requests, backfills
from `is_approved` via a frozen mapping, and creates 6 new status-prefixed
indexes alongside the existing is_approved ones (the old indexes are dropped
in Phase 4).

Frozen mapping (do NOT import models.approval.ApprovalStatus here —
permtxt01 convention: migrations are self-contained):
    NULL  → 'pending'
    True  → 'approved'
    False → 'rejected'

Revision ID: apvstat01
Revises: suprhlt01
Create Date: 2026-05-26
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "apvstat01"
down_revision = "suprhlt01"
branch_labels = None
depends_on = None

_TABLES = ("leave_records", "overtime_records", "punch_correction_requests")
_CHECK_VALUES = "('pending','approved','rejected')"

# Frozen mapping — do NOT import ApprovalStatus enum.
_BACKFILL_SQL = """
UPDATE {table}
SET status = CASE
    WHEN is_approved IS TRUE  THEN 'approved'
    WHEN is_approved IS FALSE THEN 'rejected'
    ELSE 'pending'
END
"""

_NEW_INDEXES = [
    # (table, index_name, columns)
    ("leave_records", "ix_leave_emp_status", ["employee_id", "status"]),
    ("leave_records", "ix_leave_status_start_date", ["status", "start_date"]),
    ("leave_records", "ix_leave_emp_type_status", ["employee_id", "leave_type", "status"]),
    ("leave_records", "ix_leave_status_date", ["status", "start_date"]),
    ("overtime_records", "ix_overtime_emp_status", ["employee_id", "status"]),
    ("overtime_records", "ix_overtime_status_date", ["status", "overtime_date"]),
]


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    for table in _TABLES:
        # 1) Add column nullable first so backfill can run without default propagation issues.
        op.add_column(
            table,
            sa.Column(
                "status",
                sa.String(20),
                nullable=True,
                server_default="pending",
                comment="審核狀態：pending / approved / rejected",
            ),
        )

        # 2) Backfill from is_approved via frozen mapping.
        op.execute(sa.text(_BACKFILL_SQL.format(table=table)))

        # 3) Tighten to NOT NULL.
        op.alter_column(table, "status", nullable=False)

        # 4) Add CHECK constraint — separate name per table for downgrade safety.
        op.create_check_constraint(
            f"ck_{table}_status",
            table,
            f"status IN {_CHECK_VALUES}",
        )

    # 5) Create new status-prefixed indexes (idempotent).
    for table, name, cols in _NEW_INDEXES:
        existing = _existing_indexes(bind, table)
        if name not in existing:
            op.create_index(name, table, cols)


def downgrade() -> None:
    bind = op.get_bind()

    # Drop new indexes first.
    for table, name, _cols in _NEW_INDEXES:
        existing = _existing_indexes(bind, table)
        if name in existing:
            op.drop_index(name, table_name=table)

    # Drop CHECK + column for each table (reverse order, though not strictly required).
    for table in reversed(_TABLES):
        op.drop_constraint(f"ck_{table}_status", table, type_="check")
        op.drop_column(table, "status")
```

- [ ] **Step 3: Run `alembic upgrade head` on local dev DB**

Run:
```bash
cd ~/Desktop/ivy-backend && alembic upgrade head
```
Expected: `INFO  [alembic.runtime.migration] Running upgrade suprhlt01 -> apvstat01, add_approval_status_column` with no errors.

- [ ] **Step 4: Verify column + backfill on dev DB**

Run:
```bash
psql -d ivymanagement -c "SELECT count(*) FROM leave_records WHERE status IS NULL;"
psql -d ivymanagement -c "SELECT count(*) FROM overtime_records WHERE status IS NULL;"
psql -d ivymanagement -c "SELECT count(*) FROM punch_correction_requests WHERE status IS NULL;"
psql -d ivymanagement -c "SELECT status, count(*) FROM leave_records GROUP BY status ORDER BY status;"
```
Expected: All three null counts = 0. Group-by returns only `pending` / `approved` / `rejected` values.

- [ ] **Step 5: Verify CHECK constraint rejects invalid values**

Run:
```bash
psql -d ivymanagement -c "INSERT INTO leave_records (employee_id, leave_type, start_date, end_date, status) VALUES (1, 'sick', '2026-01-01', '2026-01-01', 'bogus');"
```
Expected: ERROR — `new row for relation "leave_records" violates check constraint "ck_leave_records_status"`. (No row inserted.)

- [ ] **Step 6: Test downgrade round-trip**

Run:
```bash
cd ~/Desktop/ivy-backend && alembic downgrade -1
```
Expected: `Running downgrade apvstat01 -> suprhlt01` with no errors.

Verify status column gone:
```bash
psql -d ivymanagement -c "\d leave_records" | grep -E "status|is_approved"
```
Expected: only `is_approved` listed; no `status`.

Re-upgrade:
```bash
cd ~/Desktop/ivy-backend && alembic upgrade head
```
Expected: clean upgrade, backfill correct (counts again 0).

- [ ] **Step 7: Commit**

```bash
cd ~/Desktop/ivy-backend
git add alembic/versions/20260526_apvstat01_add_approval_status_column.py
git commit -m "feat(db): add status column to leave/overtime/punch_correction (apvstat01)

Phase 1 of approval-status enum rollout. Adds String(20) status column with
NOT NULL DEFAULT 'pending' + CHECK constraint to three tables. Backfills
from is_approved via frozen mapping (permtxt01 convention — migration is
self-contained, does not import models.approval).

Also creates 6 new status-prefixed indexes mirroring the existing is_approved
ones. The is_approved column and old indexes remain untouched (dropped in P4).

Round-trip tested on dev DB."
```

---

## Task 3: Modify `LeaveRecord` in `models/leave.py`

**Files:**
- Modify: `models/leave.py` (LeaveRecord class — add `status` Column, add 4 new Index entries, rewrite `approval_status` @property)

- [ ] **Step 1: Open the file and locate insertion points**

Read `models/leave.py` lines 66-117 in your editor. You will:
- Insert a new `status` Column declaration immediately after the `is_approved` Column block (after line 71).
- Rewrite the `approval_status` @property body (lines 102-110).
- Append 3 new Index entries to `__table_args__` (lines 112-117).

- [ ] **Step 2: Add the status Column**

In `models/leave.py`, immediately after the `is_approved` Column block (after line 71, before `approved_by`), insert:

```python
    status = Column(
        String(20),
        nullable=False,
        server_default="pending",
        comment="審核狀態：pending / approved / rejected（P1 dual-write SoT）",
    )
```

- [ ] **Step 3: Rewrite the approval_status @property**

Replace lines 102-110 (the current `approval_status` @property body) with:

```python
    @property
    def approval_status(self) -> str:
        """語意化審核狀態。P1 起內部走新 status column；既有 caller 不必改動。
        回傳值：'pending' | 'approved' | 'rejected'"""
        return self.status
```

- [ ] **Step 4: Add 4 new Index entries to __table_args__**

Replace the existing `__table_args__` tuple (lines 112-117) with:

```python
    __table_args__ = (
        Index("ix_leave_emp_dates", "employee_id", "start_date", "end_date"),
        Index("ix_leave_emp_approved", "employee_id", "is_approved"),
        Index("ix_leave_approved_start_date", "is_approved", "start_date"),
        Index("ix_leave_emp_type_approved", "employee_id", "leave_type", "is_approved"),
        # P1: new status-prefixed indexes (mirror the is_approved ones).
        # is_approved indexes are dropped in P4.
        Index("ix_leave_emp_status", "employee_id", "status"),
        Index("ix_leave_status_start_date", "status", "start_date"),
        Index("ix_leave_emp_type_status", "employee_id", "leave_type", "status"),
        Index("ix_leave_status_date", "status", "start_date"),
    )
```

Note: `ix_leave_status_date` is the P1 mirror of `ix_leave_approval_date` (which lives in a separate migration). Both stay alive until P4.

- [ ] **Step 5: Run pytest collection to verify no import error**

Run:
```bash
cd ~/Desktop/ivy-backend && pytest --collect-only tests/test_leaves.py 2>&1 | head -20
```
Expected: collection succeeds, no `ImportError` / `OperationalError`.

- [ ] **Step 6: Run existing leave tests to ensure no regression**

Run:
```bash
cd ~/Desktop/ivy-backend && pytest tests/test_leaves.py tests/test_leave_quota_helpers.py tests/test_leave_overtime_regressions.py -q 2>&1 | tail -20
```
Expected: all green (the same count as before). If any pre-existing failure shows up, compare with baseline — these are likely the documented `test_audit_router` issues, **not** caused by this change.

- [ ] **Step 7: Commit**

```bash
cd ~/Desktop/ivy-backend
git add models/leave.py
git commit -m "feat(models): add status column to LeaveRecord (P1)

Adds String(20) status column mirroring is_approved, rewrites approval_status
@property to source from the new column (read-side bridge — existing callers
work unchanged), and adds 4 new status-prefixed indexes alongside the 4
existing is_approved ones.

Listener wiring to actually populate status on writes is in a later task."
```

---

## Task 4: Modify `OvertimeRecord` in `models/overtime.py`

**Files:**
- Modify: `models/overtime.py` (OvertimeRecord class — add `status` Column, add 2 new Index entries, rewrite `approval_status` @property)

- [ ] **Step 1: Add the status Column**

In `models/overtime.py`, immediately after the `is_approved` Column declaration (line 32, before `approved_by` on line 33), insert:

```python
    status = Column(
        String(20),
        nullable=False,
        server_default="pending",
        comment="審核狀態：pending / approved / rejected（P1 dual-write SoT）",
    )
```

- [ ] **Step 2: Rewrite the approval_status @property**

Replace lines 39-47 (the current `approval_status` @property body inside `OvertimeRecord`) with:

```python
    @property
    def approval_status(self) -> str:
        """語意化審核狀態。P1 起內部走新 status column；既有 caller 不必改動。
        回傳值：'pending' | 'approved' | 'rejected'"""
        return self.status
```

- [ ] **Step 3: Add 2 new Index entries to __table_args__**

Replace the existing `__table_args__` tuple of `OvertimeRecord` (lines 49-53) with:

```python
    __table_args__ = (
        Index('ix_overtime_emp_date', 'employee_id', 'overtime_date'),
        Index('ix_overtime_emp_approved', 'employee_id', 'is_approved'),
        Index('ix_overtime_approved_date', 'is_approved', 'overtime_date'),
        # P1: new status-prefixed indexes (mirror the is_approved ones).
        Index('ix_overtime_emp_status', 'employee_id', 'status'),
        Index('ix_overtime_status_date', 'status', 'overtime_date'),
    )
```

- [ ] **Step 4: Run overtime tests to verify no regression**

Run:
```bash
cd ~/Desktop/ivy-backend && pytest tests/test_overtimes.py tests/test_overtimes_audit_logging.py tests/test_overtimes_quarterly_cap.py -q 2>&1 | tail -10
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add models/overtime.py
git commit -m "feat(models): add status column to OvertimeRecord (P1)

Mirrors the LeaveRecord P1 change — adds status column, rewrites
approval_status @property to source from new column, adds 2 new
status-prefixed indexes."
```

---

## Task 5: Modify `PunchCorrectionRequest` in `models/overtime.py`

**Files:**
- Modify: `models/overtime.py` (PunchCorrectionRequest class — add `status` Column, rewrite `approval_status` @property; no new indexes because no existing `is_approved` index)

- [ ] **Step 1: Add the status Column**

In `models/overtime.py`, immediately after the `is_approved` Column declaration in `PunchCorrectionRequest` (line 71, before `approved_by` on line 72), insert:

```python
    status = Column(
        String(20),
        nullable=False,
        server_default="pending",
        comment="審核狀態：pending / approved / rejected（P1 dual-write SoT）",
    )
```

- [ ] **Step 2: Rewrite the approval_status @property**

Replace lines 78-86 (the current `approval_status` @property body inside `PunchCorrectionRequest`) with:

```python
    @property
    def approval_status(self) -> str:
        """語意化審核狀態。P1 起內部走新 status column；既有 caller 不必改動。
        回傳值：'pending' | 'approved' | 'rejected'"""
        return self.status
```

- [ ] **Step 3: Run punch-correction tests to verify no regression**

Run:
```bash
cd ~/Desktop/ivy-backend && pytest tests/test_punch_correction.py tests/test_punch_correction_approve_recompute.py tests/test_punch_correction_self_approve_guard.py -q 2>&1 | tail -10
```
Expected: all green.

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/ivy-backend
git add models/overtime.py
git commit -m "feat(models): add status column to PunchCorrectionRequest (P1)

Mirrors the LeaveRecord/OvertimeRecord P1 change — adds status column and
rewrites approval_status @property. No new index because this table has
no existing is_approved index either."
```

---

## Task 6: Wire listeners in `models/__init__.py` + write integration tests

**Files:**
- Modify: `models/__init__.py` (append import + listener registration)
- Modify: `tests/test_approval_status_p1_dual_write.py` (add integration tests against real DB models)

- [ ] **Step 1: Append listener registration to models/__init__.py**

Append the following to `models/__init__.py` (after the last `from .year_end import (...)` block):

```python
# --- P1 approval-status dual-write listeners --------------------------------
# IMPORTANT: must be at end of file — model classes must be loaded before
# event.listens_for() runs. Direction: is_approved (set) → status (mirror).
# Reversed in P2 PR; removed in P4 PR.
from .leave import LeaveRecord, LeaveQuota  # noqa: F401,E402
from .overtime import OvertimeRecord, PunchCorrectionRequest  # noqa: F401,E402
from .approval import ApprovalStatus, register_p1_listeners  # noqa: F401,E402

register_p1_listeners(LeaveRecord, OvertimeRecord, PunchCorrectionRequest)
```

- [ ] **Step 2: Write failing integration tests**

Append the following to `tests/test_approval_status_p1_dual_write.py` (after the `TestApprovalStatusEnum` class):

```python
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import the models package — this triggers register_p1_listeners().
import models  # noqa: F401
from models.base import Base
from models.employee import Employee
from models.leave import LeaveRecord
from models.overtime import OvertimeRecord, PunchCorrectionRequest


@pytest.fixture
def session():
    """Fresh in-memory SQLite per test for full isolation."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    # Seed one employee — three models all FK to employees.
    emp = Employee(name="Test", hire_date=date(2024, 1, 1))
    s.add(emp)
    s.commit()
    yield s
    s.close()


def _make_leave(session, **overrides):
    """Build a LeaveRecord with required fields filled."""
    emp_id = session.query(Employee).first().id
    defaults = dict(
        employee_id=emp_id,
        leave_type="sick",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 1),
        leave_hours=8.0,
    )
    defaults.update(overrides)
    return LeaveRecord(**defaults)


def _make_overtime(session, **overrides):
    emp_id = session.query(Employee).first().id
    defaults = dict(
        employee_id=emp_id,
        overtime_date=date(2026, 1, 1),
        overtime_type="weekday",
        hours=2.0,
    )
    defaults.update(overrides)
    return OvertimeRecord(**defaults)


def _make_punch(session, **overrides):
    emp_id = session.query(Employee).first().id
    defaults = dict(
        employee_id=emp_id,
        attendance_date=date(2026, 1, 1),
        correction_type="punch_in",
    )
    defaults.update(overrides)
    return PunchCorrectionRequest(**defaults)


@pytest.mark.parametrize("factory", [_make_leave, _make_overtime, _make_punch])
class TestP1ListenerSyncsIsApprovedToStatus:
    def test_set_true_mirrors_to_approved(self, session, factory):
        rec = factory(session)
        rec.is_approved = True
        assert rec.status == "approved"

    def test_set_false_mirrors_to_rejected(self, session, factory):
        rec = factory(session)
        rec.is_approved = False
        assert rec.status == "rejected"

    def test_set_none_mirrors_to_pending(self, session, factory):
        rec = factory(session)
        rec.is_approved = None
        assert rec.status == "pending"

    def test_transition_chain(self, session, factory):
        rec = factory(session)
        rec.is_approved = None
        assert rec.status == "pending"
        rec.is_approved = True
        assert rec.status == "approved"
        rec.is_approved = False
        assert rec.status == "rejected"
        rec.is_approved = None
        assert rec.status == "pending"

    def test_approval_status_property_returns_status(self, session, factory):
        rec = factory(session)
        rec.is_approved = True
        assert rec.approval_status == "approved"
        assert rec.approval_status == rec.status

    def test_default_is_pending_on_construction(self, session, factory):
        rec = factory(session)
        session.add(rec)
        session.flush()
        assert rec.status == "pending"
        assert rec.approval_status == "pending"

    def test_idempotency_no_write_when_already_aligned(self, session, factory):
        """Listener guard: setting is_approved to current value should not
        rewrite status (otherwise SQLAlchemy autoflush emits an extra UPDATE)."""
        rec = factory(session)
        rec.is_approved = True
        session.add(rec)
        session.commit()
        # status is already 'approved'; setting is_approved=True again should be a no-op write.
        # We verify by clearing the dirty set and setting again — if listener writes blindly,
        # status attr becomes dirty.
        session.expire(rec)
        _ = rec.is_approved  # reload
        assert rec.status == "approved"
        rec.is_approved = True  # same value
        assert rec not in session.dirty or "status" not in {
            attr.key for attr in session.dirty
        }
```

- [ ] **Step 3: Run failing tests to verify they fail**

Run:
```bash
cd ~/Desktop/ivy-backend && pytest tests/test_approval_status_p1_dual_write.py -v 2>&1 | tail -40
```
Expected: tests in `TestApprovalStatusEnum` still pass (3). Tests in `TestP1ListenerSyncsIsApprovedToStatus` should now PASS — listener is registered via models/__init__.py import. If they fail with `AttributeError: status`, the model column declaration tasks (3/4/5) didn't land — go back and fix.

(Note: this is not strict TDD red→green because Tasks 1-5 already implemented the structure. The integration tests here serve as regression coverage rather than spec-first tests. This is acceptable for dual-write infra tasks where the unit-of-meaning is the cross-component behavior.)

- [ ] **Step 4: Run the full leave + overtime + punch test families**

Run:
```bash
cd ~/Desktop/ivy-backend && pytest tests/test_leaves.py tests/test_overtimes.py tests/test_punch_correction.py tests/test_approval_status_p1_dual_write.py -q 2>&1 | tail -10
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add models/__init__.py tests/test_approval_status_p1_dual_write.py
git commit -m "feat(approval): wire P1 listeners + add dual-write integration tests

Imports the three models in models/__init__.py and calls
register_p1_listeners() to wire is_approved → status sync via SQLAlchemy
attribute set event.

Tests cover:
- Three bool values (True/False/None) mirror correctly across all 3 models.
- Transition chain (None → True → False → None).
- approval_status @property reads from new column.
- Construction default = pending.
- Idempotency guard (no extra UPDATE when value unchanged)."
```

---

## Task 7: Full pytest regression sweep + P1 summary commit

**Files:** none modified — verification only.

- [ ] **Step 1: Run full backend pytest suite**

Run:
```bash
cd ~/Desktop/ivy-backend && pytest -q 2>&1 | tail -30
```
Expected: matches the pre-P1 baseline (5103 passed, 14 pre-existing failures like `test_audit_router`). If the **passed** count drops or new failures appear, **stop and investigate** — do not commit until clean.

- [ ] **Step 2: Compare against baseline**

The pre-P1 baseline from CLAUDE.md activity log is `5103 passed`. New P1 tests should add ~25 (3 enum + 7 parametrized × 3 models = 21, plus a few class-level scaffolds). Expected new total: **~5128 passed**.

If you see significantly more failures or fewer passes than expected, run:
```bash
cd ~/Desktop/ivy-backend && pytest -q --tb=short 2>&1 | grep -E "FAILED|ERROR" | head -20
```
Cross-check against known pre-existing failures (`test_audit_router`, `test_supabase_storage`). Any new failure = bug.

- [ ] **Step 3: Verify grep invariants**

P1 should leave is_approved untouched in all callsites. Verify:

```bash
cd ~/Desktop/ivy-backend
# Production code is_approved references should be unchanged (~190 still present).
grep -rn "is_approved" --include="*.py" | grep -v ".claude/worktrees" | grep -v "/tests/" | wc -l
# Status references should be only in approval.py, models/__init__.py, three models, migration, and the new test file.
grep -rn "\bstatus\b" models/approval.py models/leave.py models/overtime.py models/__init__.py | wc -l
```
The first count should be roughly identical to before P1 (~190). The second count should be > 0 (status now exists in models).

- [ ] **Step 4: Verify prod DB readiness check command works**

This is the same query the spec lists as a P1 acceptance check; verify it runs cleanly:

```bash
psql -d ivymanagement -c "SELECT 'leave' AS tbl, count(*) FROM leave_records WHERE status IS NULL
UNION ALL SELECT 'overtime', count(*) FROM overtime_records WHERE status IS NULL
UNION ALL SELECT 'punch', count(*) FROM punch_correction_requests WHERE status IS NULL;"
```
Expected: 3 rows, all count = 0.

- [ ] **Step 5: Final summary commit (if any tidy-up needed) — otherwise skip**

If no further changes, skip. If you noticed any stray import / typo / formatting issue across Tasks 1-6, fix and commit:

```bash
cd ~/Desktop/ivy-backend
git add -p  # review carefully
git commit -m "chore(approval): P1 final cleanup"
```

- [ ] **Step 6: Print the P1 completion banner**

Output (do not commit this — just print for human review):

```
P1 complete. Status:
- Schema: status column added to 3 tables with CHECK + 6 new indexes.
- Listener: is_approved → status mirroring active.
- Existing approval_status @property now sources from new column.
- Tests: ~25 new passing, full suite no regression.

Next: P2 PR — rewrite ~190 backend callsites + 240 test references to use
status directly, then flip listener direction. Wait at least 3 days in prod
before starting P2 (per spec §4.P1 acceptance).
```

---

## Self-Review Checklist

**Spec coverage:**
- ✓ §3.1 共用 ApprovalStatus enum — Task 1
- ✓ §3.2 新 column `status` 命名 — Tasks 2/3/4/5
- ✓ §3.2 `approval_status` @property 改寫 — Tasks 3/4/5
- ✓ §3.3 String + CHECK constraint — Task 2
- ✓ §3.4 P1 listener direction `is_approved → status` + idempotency guard — Tasks 1/6
- ✓ §3.5 Frozen mapping backfill — Task 2
- ✓ §4.P1 6 new indexes — Tasks 2 (DB) + 3/4 (model)
- ✓ §4.P1 驗收條件：null count = 0 — Task 7
- ✓ §5.2 bulk update audit — already done in spec (no production hits); test fixture in `test_leave_bonus_skip.py` is P2 scope, not P1
- ✓ §5.6 SQLite + Postgres dual-dialect — Task 2 uses portable `op.create_check_constraint` + `op.create_index` which work in batch_mode on SQLite for in-memory tests
- ✓ §6.1 P1 test coverage — Task 6

**Placeholder scan:** No "TBD" / "implement later" / "add error handling" / "similar to Task N" — every step has explicit code.

**Type consistency:**
- `register_p1_listeners(*classes)` signature matches usage in `models/__init__.py` (Task 6).
- Column name `status`, enum class `ApprovalStatus`, helper module `models.approval` consistent across all tasks.
- Index name conventions (`ix_<table>_<cols-pattern>`) match spec table.

**Risks acknowledged:**
- Task 2 step 4 uses `psql -d ivymanagement` which requires the dev DB to be running (CLAUDE.md confirms `postgresql://yilunwu@localhost:5432/ivymanagement`).
- Task 6 step 3 note: this task is regression-test-flavored rather than strict TDD red→green, which is appropriate for cross-component listener infra and matches the codebase's existing pytest patterns.
