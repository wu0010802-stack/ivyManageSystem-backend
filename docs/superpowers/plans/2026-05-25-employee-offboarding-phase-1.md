# 員工離職 Checklist Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立員工離職 checklist 統一編排層的 Phase 1 後端基礎（migration + orchestrator + 4 step + 5 endpoint），救火 aggregator dashboard 離職員工消失問題、為 Phase 2 PDF 與 magic-link 準備好框架。

**Architecture:** 新 `services/offboarding/` 模組以 SQLAlchemy 單一 transaction 編排 4 step（mark_appraisal / snapshot_leave / prefill_leave_payout / revoke_user），失敗整筆 rollback。新 `api/offboarding.py` router 提供 preview/process/get/nhi-unenroll 4 endpoint，舊 `POST /employees/{id}/resign` 改 deprecation passthrough。修 `services/appraisal/status_aggregator.py:463` filter 條件納入當期離職員工。

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic v2, Alembic, pytest, PostgreSQL（dev 本機 / SQLite for tests）。

**Spec：** `docs/superpowers/specs/2026-05-25-employee-offboarding-checklist-design.md`（已 commit `52b1b77`）。

**Phase 1 不含：** 離職證明 PDF（Phase 2）、magic-link（Phase 2）、ZIP download（Phase 2）、前端 UI（Phase 3）。

---

## 檔案結構

新建檔：
- `alembic/versions/offb0001_employee_offboarding_records.py` — migration
- `models/offboarding.py` — `EmployeeOffboardingRecord` model
- `utils/leave_quota_helpers.py` — 特休餘額計算 public function
- `services/offboarding/__init__.py`
- `services/offboarding/orchestrator.py` — `process_offboarding()` 主入口 + types
- `services/offboarding/steps/__init__.py`
- `services/offboarding/steps/mark_appraisal.py`
- `services/offboarding/steps/snapshot_leave.py`
- `services/offboarding/steps/revoke_user.py`
- `api/offboarding.py` — 5 endpoint router
- `schemas/offboarding.py` — Pydantic request/response models

修改檔：
- `models/salary.py:169-` — `SalaryRecord` 加 `unused_leave_payout` 欄
- `services/appraisal/status_aggregator.py:451-465` — filter 條件改納入當期離職員工
- `api/employees.py:759-824` — 舊 `/employees/{id}/resign` 改 passthrough 呼叫 orchestrator
- `main.py` — 註冊 `offboarding_router`
- `utils/sentry_init.py` — PII denylist 加 `resign_reason`、`leave_balance_snapshot`、`certificate_pdf_path`

新建測試：
- `tests/test_offboarding_migration.py`
- `tests/test_leave_quota_helpers.py`
- `tests/test_offboarding_step_mark_appraisal.py`
- `tests/test_offboarding_step_snapshot_leave.py`
- `tests/test_offboarding_step_revoke_user.py`
- `tests/test_offboarding_orchestrator.py`
- `tests/test_offboarding_api.py`
- `tests/test_appraisal_aggregator_offboarding.py`
- `tests/test_offboarding_legacy_endpoint_passthrough.py`

---

## Task 1: Migration `offb0001` + Model

**Files:**
- Create: `alembic/versions/offb0001_employee_offboarding_records.py`
- Create: `models/offboarding.py`
- Modify: `models/salary.py:230-250`（在 `appraisal_year_end_bonus` 之後加 `unused_leave_payout`）
- Modify: `models/__init__.py`（若 explicit export，加入 EmployeeOffboardingRecord）
- Test: `tests/test_offboarding_migration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_offboarding_migration.py
"""驗證 offb0001 migration 建立表結構與 SalaryRecord.unused_leave_payout 欄。"""
from sqlalchemy import inspect, create_engine
from alembic.config import Config
from alembic import command
import os


def _run_migrations(db_url: str) -> None:
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def test_offb0001_creates_table_and_indexes(tmp_path):
    db_path = tmp_path / "offb_test.db"
    db_url = f"sqlite:///{db_path}"
    _run_migrations(db_url)

    engine = create_engine(db_url)
    insp = inspect(engine)

    assert "employee_offboarding_records" in insp.get_table_names()

    cols = {c["name"] for c in insp.get_columns("employee_offboarding_records")}
    expected_cols = {
        "employee_id", "resign_date", "resign_reason",
        "opened_at", "opened_by_user_id",
        "user_revoked_at", "appraisal_marked_at",
        "leave_snapshot_at", "certificate_generated_at",
        "leave_balance_snapshot", "certificate_pdf_path",
        "nhi_unenroll_submitted_at",
        "magic_link_token_hash", "magic_link_expires_at",
        "magic_link_revoked_at", "magic_link_download_count",
        "magic_link_last_used_at",
        "closed_at", "closed_by_user_id",
    }
    assert expected_cols.issubset(cols), f"missing: {expected_cols - cols}"

    indexes = {i["name"] for i in insp.get_indexes("employee_offboarding_records")}
    assert "ix_offboarding_resign_date" in indexes
    assert "ix_offboarding_open_status" in indexes


def test_offb0001_adds_unused_leave_payout_column(tmp_path):
    db_path = tmp_path / "offb_test.db"
    db_url = f"sqlite:///{db_path}"
    _run_migrations(db_url)

    engine = create_engine(db_url)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("salary_records")}
    assert "unused_leave_payout" in cols


def test_offb0001_downgrade_drops_table_and_column(tmp_path):
    db_path = tmp_path / "offb_test.db"
    db_url = f"sqlite:///{db_path}"
    _run_migrations(db_url)

    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.downgrade(cfg, "-1")

    engine = create_engine(db_url)
    insp = inspect(engine)
    assert "employee_offboarding_records" not in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("salary_records")}
    assert "unused_leave_payout" not in cols
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ivy-backend && pytest tests/test_offboarding_migration.py -v
```

Expected: FAIL — migration `offb0001` 尚未存在。

- [ ] **Step 3: Create the model**

```python
# models/offboarding.py
"""models/offboarding.py — 員工離職 checklist 模型"""

from datetime import datetime

from sqlalchemy import (
    Column, Integer, Text, Date, DateTime, ForeignKey, Index, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base


class EmployeeOffboardingRecord(Base):
    """員工離職 checklist 紀錄（one-to-one with Employee）"""

    __tablename__ = "employee_offboarding_records"

    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="CASCADE"),
        primary_key=True,
    )
    resign_date = Column(Date, nullable=False)
    resign_reason = Column(Text, nullable=True)

    opened_at = Column(DateTime, nullable=False)
    opened_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    user_revoked_at = Column(DateTime, nullable=True)
    appraisal_marked_at = Column(DateTime, nullable=True)
    leave_snapshot_at = Column(DateTime, nullable=True)
    certificate_generated_at = Column(DateTime, nullable=True)

    leave_balance_snapshot = Column(JSONB, nullable=True)
    certificate_pdf_path = Column(Text, nullable=True)
    nhi_unenroll_submitted_at = Column(DateTime, nullable=True)

    magic_link_token_hash = Column(Text, nullable=True)
    magic_link_expires_at = Column(DateTime, nullable=True)
    magic_link_revoked_at = Column(DateTime, nullable=True)
    magic_link_download_count = Column(Integer, default=0, nullable=False)
    magic_link_last_used_at = Column(DateTime, nullable=True)

    closed_at = Column(DateTime, nullable=True)
    closed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    employee = relationship(
        "Employee",
        back_populates="offboarding_record",
    )

    __table_args__ = (
        Index("ix_offboarding_resign_date", "resign_date"),
        Index(
            "ix_offboarding_open_status",
            "closed_at",
            postgresql_where=text("closed_at IS NULL"),
            sqlite_where=text("closed_at IS NULL"),
        ),
    )
```

**並修改 `models/employee.py`** 在 `class Employee` 內加反向 relationship（找既有 relationship block，仿同樣風格）：

```python
    offboarding_record = relationship(
        "EmployeeOffboardingRecord",
        uselist=False,
        back_populates="employee",
        cascade="all, delete-orphan",
    )
```

並在 `models/employee.py` 頂部 import 區末尾加（若 lazy import 慣例已存在則跳過）：

```python
# 避免循環 import：EmployeeOffboardingRecord 在 models/offboarding.py
# SQLAlchemy 用字串 "EmployeeOffboardingRecord" 解析，無需直接 import
```

- [ ] **Step 4: Modify `models/salary.py` to add `unused_leave_payout`**

在 `class SalaryRecord` 找到 `appraisal_year_end_bonus = Column(...)` 那行附近（約 line 242-248），在其後加：

```python
    unused_leave_payout = Column(
        Money,
        default=0,
        nullable=False,
        server_default="0",
        comment="特休未休折現（§38；獨立 column 不進 gross_salary，仿 appraisal_year_end_bonus）",
    )
```

`Money` 是檔內既有 type alias（檢查 import 確認），預設值 0 兼容既有 row。

- [ ] **Step 5: Create the migration**

```python
# alembic/versions/offb0001_employee_offboarding_records.py
"""employee_offboarding_records + SalaryRecord.unused_leave_payout

Revision ID: offb0001
Revises: mergeheads02
Create Date: 2026-05-25 ...

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "offb0001"
down_revision: Union[str, Sequence[str], None] = "mergeheads02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employee_offboarding_records",
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("resign_date", sa.Date(), nullable=False),
        sa.Column("resign_reason", sa.Text(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("opened_by_user_id", sa.Integer(), nullable=False),
        sa.Column("user_revoked_at", sa.DateTime(), nullable=True),
        sa.Column("appraisal_marked_at", sa.DateTime(), nullable=True),
        sa.Column("leave_snapshot_at", sa.DateTime(), nullable=True),
        sa.Column("certificate_generated_at", sa.DateTime(), nullable=True),
        sa.Column(
            "leave_balance_snapshot",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
        sa.Column("certificate_pdf_path", sa.Text(), nullable=True),
        sa.Column("nhi_unenroll_submitted_at", sa.DateTime(), nullable=True),
        sa.Column("magic_link_token_hash", sa.Text(), nullable=True),
        sa.Column("magic_link_expires_at", sa.DateTime(), nullable=True),
        sa.Column("magic_link_revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "magic_link_download_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("magic_link_last_used_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("closed_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["employee_id"], ["employees.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["opened_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["closed_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("employee_id"),
    )
    op.create_index(
        "ix_offboarding_resign_date",
        "employee_offboarding_records",
        ["resign_date"],
    )

    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.create_index(
            "ix_offboarding_open_status",
            "employee_offboarding_records",
            ["closed_at"],
            postgresql_where=sa.text("closed_at IS NULL"),
        )
    else:
        op.create_index(
            "ix_offboarding_open_status",
            "employee_offboarding_records",
            ["closed_at"],
            sqlite_where=sa.text("closed_at IS NULL"),
        )

    op.add_column(
        "salary_records",
        sa.Column(
            "unused_leave_payout",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
            comment="特休未休折現（§38；獨立 column 不進 gross_salary）",
        ),
    )


def downgrade() -> None:
    op.drop_column("salary_records", "unused_leave_payout")
    op.drop_index("ix_offboarding_open_status", "employee_offboarding_records")
    op.drop_index("ix_offboarding_resign_date", "employee_offboarding_records")
    op.drop_table("employee_offboarding_records")
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd ivy-backend && pytest tests/test_offboarding_migration.py -v
```

Expected: PASS 3 tests。

- [ ] **Step 7: Run full pytest to verify no regression**

```bash
cd ivy-backend && pytest -x --ignore=tests/test_audit_router.py --ignore=tests/test_supabase_storage.py 2>&1 | tail -20
```

Expected: PASS（test_audit_router / test_supabase_storage 已知 pre-existing fail，跳過）。

- [ ] **Step 8: Commit**

```bash
cd ivy-backend && git add alembic/versions/offb0001_employee_offboarding_records.py \
  models/offboarding.py models/salary.py tests/test_offboarding_migration.py && \
git commit -m "$(cat <<'EOF'
feat(offboarding): add offb0001 migration + EmployeeOffboardingRecord model

新建 employee_offboarding_records 表（PK=employee_id，one-to-one Employee）
記離職 checklist 狀態：4 個 step timestamp、leave_balance_snapshot JSONB、
certificate_pdf_path、nhi_unenroll_submitted_at、magic_link 5 欄、closure。

partial index ix_offboarding_open_status 加速「未結案 checklist」查詢。

一併加 SalaryRecord.unused_leave_payout 新欄（仿 appraisal_year_end_bonus
獨立 column 不進 gross_salary），供特休未休折現結算寫入。

Phase 1 of 員工離職 checklist；spec: docs/superpowers/specs/2026-05-25-employee-offboarding-checklist-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 特休餘額計算 public helper

**Files:**
- Create: `utils/leave_quota_helpers.py`
- Test: `tests/test_leave_quota_helpers.py`

**動機：** snapshot_leave step 需算「離職當下特休餘額」。既有 `api/leaves_quota.py:234` `_get_used_hours()` 是 private 不該跨檔 import；抽 public function 供 orchestrator 與既有 endpoint 共用（後者可作為 follow-up 替換）。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_leave_quota_helpers.py
"""驗證 get_annual_leave_balance 公式 = quota.total_hours - approved_used_hours。"""
from datetime import date, datetime
import pytest

from utils.leave_quota_helpers import get_annual_leave_balance


def test_returns_zero_when_no_quota(db_session, employee_factory):
    emp = employee_factory()
    result = get_annual_leave_balance(db_session, emp.id, date(2026, 6, 15))
    assert result == {
        "total_hours": 0.0,
        "used_hours": 0.0,
        "remaining_hours": 0.0,
        "remaining_days": 0.0,
        "snapshot_date": date(2026, 6, 15),
    }


def test_calculates_remaining_from_quota_minus_approved(
    db_session, employee_factory, leave_quota_factory, leave_record_factory
):
    emp = employee_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )  # 14 天
    leave_record_factory(
        employee_id=emp.id,
        leave_type="annual",
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 9),  # 含週末省略，假設純功能
        leave_hours=72,  # 9 天
        status="approved",
    )
    result = get_annual_leave_balance(db_session, emp.id, date(2026, 6, 15))
    assert result["total_hours"] == 112.0
    assert result["used_hours"] == 72.0
    assert result["remaining_hours"] == 40.0
    assert result["remaining_days"] == 5.0


def test_excludes_pending_records(
    db_session, employee_factory, leave_quota_factory, leave_record_factory
):
    """只算 approved；pending 不扣（離職時 pending 假應由 admin 處理）"""
    emp = employee_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    leave_record_factory(
        employee_id=emp.id,
        leave_type="annual",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 1),
        leave_hours=8,
        status="pending",
    )
    result = get_annual_leave_balance(db_session, emp.id, date(2026, 6, 15))
    assert result["used_hours"] == 0.0
    assert result["remaining_hours"] == 80.0


def test_uses_school_year_when_present(
    db_session, employee_factory, leave_quota_factory
):
    """leave_quotas 同 employee 同 year 可能有 legacy + school_year 兩 row；採 school_year 優先"""
    emp = employee_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual",
        total_hours=80, school_year=None,
    )
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual",
        total_hours=112, school_year=115,  # 民國 115 = 2026 學年
    )
    result = get_annual_leave_balance(db_session, emp.id, date(2026, 6, 15))
    assert result["total_hours"] == 112.0  # school_year row 優先
```

**注意：** 上面 fixtures `employee_factory`、`leave_quota_factory`、`leave_record_factory` 假設 `tests/conftest.py` 有定義；若無，先 grep `conftest.py` 確認既有 fixture 名稱，或在 test file 內 inline 建 model。

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ivy-backend && pytest tests/test_leave_quota_helpers.py -v
```

Expected: FAIL — `utils.leave_quota_helpers` ImportError。

- [ ] **Step 3: Implement helper**

```python
# utils/leave_quota_helpers.py
"""特休（annual leave）餘額計算 public helper。

從 api/leaves_quota.py:_get_used_hours 抽出供 services/offboarding/ 與其他模組共用。
公式：remaining_hours = quota.total_hours - approved_used_hours
（不含 pending；離職時 pending 應由 admin 先處理）
"""
from datetime import date
from sqlalchemy import func
from sqlalchemy.orm import Session

from models.leave import LeaveQuota, LeaveRecord


def get_annual_leave_balance(
    session: Session,
    employee_id: int,
    snapshot_date: date,
) -> dict:
    """回傳指定員工於 snapshot_date 當日特休餘額。

    Returns:
        dict with keys:
        - total_hours: float       quota.total_hours（school_year row 優先）
        - used_hours: float        已 approved 的 annual hours
        - remaining_hours: float   = total - used，下限 0
        - remaining_days: float    = remaining_hours / 8
        - snapshot_date: date      傳入值，回傳供 audit
    """
    year = snapshot_date.year

    quota = (
        session.query(LeaveQuota)
        .filter(
            LeaveQuota.employee_id == employee_id,
            LeaveQuota.year == year,
            LeaveQuota.leave_type == "annual",
            LeaveQuota.school_year.isnot(None),
        )
        .first()
    )
    if quota is None:
        quota = (
            session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == employee_id,
                LeaveQuota.year == year,
                LeaveQuota.leave_type == "annual",
                LeaveQuota.school_year.is_(None),
            )
            .first()
        )

    total_hours = float(quota.total_hours) if quota else 0.0

    used_hours = (
        session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0.0))
        .filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.leave_type == "annual",
            LeaveRecord.status == "approved",
            LeaveRecord.start_date >= date(year, 1, 1),
            LeaveRecord.start_date <= snapshot_date,
        )
        .scalar()
    ) or 0.0
    used_hours = float(used_hours)

    remaining_hours = max(0.0, total_hours - used_hours)
    remaining_days = round(remaining_hours / 8.0, 2)

    return {
        "total_hours": total_hours,
        "used_hours": used_hours,
        "remaining_hours": remaining_hours,
        "remaining_days": remaining_days,
        "snapshot_date": snapshot_date,
    }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ivy-backend && pytest tests/test_leave_quota_helpers.py -v
```

Expected: PASS 4 tests。若 fixture 不存在需先建（檢查 `tests/conftest.py`）。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend && git add utils/leave_quota_helpers.py tests/test_leave_quota_helpers.py && \
git commit -m "feat(leave): add get_annual_leave_balance public helper for offboarding snapshot

公式 quota.total_hours - approved_used_hours；school_year row 優先。
供 services/offboarding/ snapshot_leave step 與既有 api/leaves_quota.py
（後續可替換 _get_used_hours 內部實作）共用。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Orchestrator types + 殼

**Files:**
- Create: `services/offboarding/__init__.py`（空）
- Create: `services/offboarding/orchestrator.py`
- Create: `services/offboarding/steps/__init__.py`（空）
- Test: `tests/test_offboarding_orchestrator.py`（建檔，初版只測 types）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_offboarding_orchestrator.py
"""驗證 orchestrator types + 主入口 signature。"""
from datetime import date

from services.offboarding.orchestrator import (
    process_offboarding,
    OffboardingResult,
    StepResult,
    OffboardingError,
)


def test_step_result_typeddict_fields():
    sr: StepResult = {
        "step": "mark_appraisal",
        "status": "completed",
        "completed_at": None,
        "payload": None,
        "error": None,
    }
    assert sr["step"] == "mark_appraisal"


def test_offboarding_error_subclass_of_exception():
    err = OffboardingError("test", code="LEAVE_BALANCE_NOT_FOUND")
    assert isinstance(err, Exception)
    assert err.code == "LEAVE_BALANCE_NOT_FOUND"


def test_process_offboarding_creates_record_with_only_default_step_unimplemented(
    db_session, employee_factory, user_factory, monkeypatch
):
    """初版 orchestrator 殼：無 step 註冊 → 建 record + 空 steps list。
    後續 task 加 step 後此測試會被覆寫；現在只驗框架可呼叫。"""
    emp = employee_factory()
    user = user_factory()
    result = process_offboarding(
        session=db_session,
        employee_id=emp.id,
        resign_date=date(2026, 6, 15),
        resign_reason="test",
        operator_user_id=user.id,
    )
    assert result["employee_id"] == emp.id
    assert result["resign_date"] == date(2026, 6, 15)
    assert isinstance(result["steps"], list)

    from models.offboarding import EmployeeOffboardingRecord
    record = db_session.query(EmployeeOffboardingRecord).filter_by(
        employee_id=emp.id
    ).first()
    assert record is not None
    assert record.opened_by_user_id == user.id
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ivy-backend && pytest tests/test_offboarding_orchestrator.py -v
```

Expected: FAIL — `services.offboarding.orchestrator` ImportError。

- [ ] **Step 3: Implement orchestrator shell**

```python
# services/offboarding/__init__.py
"""員工離職 checklist 編排層。"""
```

```python
# services/offboarding/steps/__init__.py
"""離職 checklist 個別 step 實作。"""
```

```python
# services/offboarding/orchestrator.py
"""一鍵離職主入口：單一 transaction 串接 4 step（Phase 1）。

設計參考：docs/superpowers/specs/2026-05-25-employee-offboarding-checklist-design.md §5
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, TypedDict

from sqlalchemy.orm import Session

from models.offboarding import EmployeeOffboardingRecord
from models.employee import Employee

logger = logging.getLogger(__name__)


class StepResult(TypedDict):
    step: str
    status: Literal["completed", "skipped", "failed"]
    completed_at: datetime | None
    payload: dict | None
    error: str | None


class OffboardingResult(TypedDict):
    employee_id: int
    resign_date: date
    is_active_after: bool
    user_account_revoked: bool
    steps: list[StepResult]
    certificate_pdf_path: str | None  # Phase 2 才填，Phase 1 一律 None


class OffboardingError(Exception):
    """離職流程錯誤。code 對應 API HTTP detail。"""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def process_offboarding(
    session: Session,
    employee_id: int,
    resign_date: date,
    resign_reason: str | None,
    operator_user_id: int,
) -> OffboardingResult:
    """一鍵離職主入口。

    Args:
        session: SQLAlchemy session（呼叫端負責 commit / rollback）
        employee_id: 對象員工
        resign_date: 離職日（可 > today 為通知期）
        resign_reason: 離職原因（不寫入證明 PDF）
        operator_user_id: 操作 admin User.id（寫入 opened_by_user_id）

    Returns:
        OffboardingResult dict

    Raises:
        OffboardingError: 任一 step 失敗，呼叫端必須 rollback session
        ValueError: input 驗證失敗（hire_date / resign_date 異常）
    """
    emp = session.query(Employee).filter_by(id=employee_id).first()
    if emp is None:
        raise OffboardingError("員工不存在", code="EMPLOYEE_NOT_FOUND")

    existing = (
        session.query(EmployeeOffboardingRecord)
        .filter_by(employee_id=employee_id)
        .first()
    )
    if existing is not None:
        raise OffboardingError(
            f"員工 {employee_id} 已有離職紀錄 (resign_date={existing.resign_date})",
            code="ALREADY_OFFBOARDED",
        )

    if emp.hire_date and resign_date < emp.hire_date:
        raise OffboardingError(
            f"resign_date {resign_date} 早於 hire_date {emp.hire_date}",
            code="RESIGN_DATE_BEFORE_HIRE",
        )

    today = date.today()
    if (resign_date - today).days > 90:
        raise OffboardingError(
            f"resign_date {resign_date} 超過 today + 90 天",
            code="RESIGN_DATE_TOO_FAR_FUTURE",
        )

    record = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=resign_date,
        resign_reason=resign_reason,
        opened_at=datetime.now(),
        opened_by_user_id=operator_user_id,
    )
    session.add(record)
    session.flush()  # FK 取得，但不 commit

    # 寫入 Employee.resign_date / resign_reason（既有 employee.py:765-766 行為對齊）
    emp.resign_date = resign_date
    emp.resign_reason = resign_reason
    if resign_date <= today:
        emp.is_active = False

    steps_result: list[StepResult] = []
    # Phase 1 4 step 會於後續 task 加入：mark_appraisal, snapshot_leave,
    # prefill_leave_payout, revoke_user. 此 orchestrator 殼初版 steps 為空。
    # Phase 2 加 generate_certificate 為第 5 step。

    return OffboardingResult(
        employee_id=employee_id,
        resign_date=resign_date,
        is_active_after=emp.is_active,
        user_account_revoked=False,  # revoke_user step 加入後修
        steps=steps_result,
        certificate_pdf_path=None,
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ivy-backend && pytest tests/test_offboarding_orchestrator.py -v
```

Expected: PASS 3 tests。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend && git add services/offboarding/ tests/test_offboarding_orchestrator.py && \
git commit -m "feat(offboarding): orchestrator shell + types (process_offboarding)

主入口 process_offboarding(session, employee_id, resign_date, resign_reason,
operator_user_id) 建 EmployeeOffboardingRecord + 設 Employee.resign_date/is_active；
input 驗證 EMPLOYEE_NOT_FOUND / ALREADY_OFFBOARDED / RESIGN_DATE_BEFORE_HIRE /
RESIGN_DATE_TOO_FAR_FUTURE 4 種 OffboardingError。

types: StepResult, OffboardingResult TypedDict + OffboardingError(code).
step 串接於後續 task 加入。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Step — mark_appraisal

**Files:**
- Create: `services/offboarding/steps/mark_appraisal.py`
- Test: `tests/test_offboarding_step_mark_appraisal.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_offboarding_step_mark_appraisal.py
"""驗證 mark_appraisal step：純寫 timestamp，不會失敗。"""
from datetime import date, datetime

from services.offboarding.steps.mark_appraisal import run
from models.offboarding import EmployeeOffboardingRecord


def test_mark_appraisal_writes_timestamp(db_session, employee_factory, user_factory):
    emp = employee_factory()
    user = user_factory()
    record = EmployeeOffboardingRecord(
        employee_id=emp.id,
        resign_date=date(2026, 6, 15),
        opened_at=datetime.now(),
        opened_by_user_id=user.id,
    )
    db_session.add(record)
    db_session.flush()

    result = run(db_session, record)
    assert result["step"] == "mark_appraisal"
    assert result["status"] == "completed"
    assert result["completed_at"] is not None
    assert record.appraisal_marked_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ivy-backend && pytest tests/test_offboarding_step_mark_appraisal.py -v
```

Expected: FAIL — `services.offboarding.steps.mark_appraisal` ImportError。

- [ ] **Step 3: Implement**

```python
# services/offboarding/steps/mark_appraisal.py
"""mark_appraisal step：純寫 appraisal_marked_at audit timestamp。

aggregator filter 改條件後（task 14），離職員工自動繼續出現在當期 cycle，
此 step 本身不動 appraisal 資料，只留 audit timestamp 標記「此員工的離職事件
已被系統標記，後續 dashboard 仍會顯示」。
"""
from datetime import datetime
from sqlalchemy.orm import Session

from models.offboarding import EmployeeOffboardingRecord
from services.offboarding.orchestrator import StepResult


def run(session: Session, record: EmployeeOffboardingRecord) -> StepResult:
    now = datetime.now()
    record.appraisal_marked_at = now
    return {
        "step": "mark_appraisal",
        "status": "completed",
        "completed_at": now,
        "payload": None,
        "error": None,
    }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ivy-backend && pytest tests/test_offboarding_step_mark_appraisal.py -v
```

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend && git add services/offboarding/steps/mark_appraisal.py \
  tests/test_offboarding_step_mark_appraisal.py && \
git commit -m "feat(offboarding): step mark_appraisal — write audit timestamp

純寫 record.appraisal_marked_at = now()；不動 appraisal 資料。
aggregator filter（task 14）改條件後離職員工繼續出現於當期 cycle，此 step
只留 audit 記錄系統已標記。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Step — snapshot_leave

**Files:**
- Create: `services/offboarding/steps/snapshot_leave.py`
- Test: `tests/test_offboarding_step_snapshot_leave.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_offboarding_step_snapshot_leave.py
"""驗證 snapshot_leave step：寫 leave_balance_snapshot JSONB + leave_snapshot_at。"""
from datetime import date, datetime
import pytest

from services.offboarding.steps.snapshot_leave import run
from services.offboarding.orchestrator import OffboardingError
from models.offboarding import EmployeeOffboardingRecord


def _make_record(db_session, employee_id, user_id):
    record = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=date(2026, 6, 15),
        opened_at=datetime.now(),
        opened_by_user_id=user_id,
    )
    db_session.add(record)
    db_session.flush()
    return record


def test_snapshot_writes_balance_to_jsonb(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    emp = employee_factory(daily_wage=1800)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    record = _make_record(db_session, emp.id, user.id)

    result = run(db_session, record)

    assert result["step"] == "snapshot_leave"
    assert result["status"] == "completed"
    assert record.leave_snapshot_at is not None
    snap = record.leave_balance_snapshot
    assert snap["total_hours"] == 112.0
    assert snap["used_hours"] == 0.0
    assert snap["remaining_hours"] == 112.0
    assert snap["remaining_days"] == 14.0
    assert snap["daily_wage"] == 1800.0
    assert snap["payout_amount"] == 14 * 1800  # 25200
    assert snap["calc_rule_version"] == "labor_act_38_2026_v1"
    assert result["payload"] == {"days": 14.0, "payout": 25200.0}


def test_snapshot_when_no_quota_returns_zero(
    db_session, employee_factory, user_factory
):
    """無 quota row 不算失敗：員工可能剛到職未生 quota，snapshot 寫 0"""
    emp = employee_factory(daily_wage=1500)
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    result = run(db_session, record)
    assert result["status"] == "completed"
    assert record.leave_balance_snapshot["remaining_days"] == 0.0
    assert record.leave_balance_snapshot["payout_amount"] == 0.0


def test_snapshot_raises_when_employee_has_no_daily_wage(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """daily_wage 缺失或 0 → 422 LEAVE_BALANCE_NOT_FOUND（折現需 daily_wage）"""
    emp = employee_factory(daily_wage=None)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    record = _make_record(db_session, emp.id, user.id)

    with pytest.raises(OffboardingError) as exc_info:
        run(db_session, record)
    assert exc_info.value.code == "LEAVE_BALANCE_NOT_FOUND"
```

**注意：** `Employee.daily_wage` 欄位實際名稱可能是 `daily_salary` 或計算自 `monthly_salary / 30`。先 grep `models/employee.py` 確認。若無 daily 直接欄，計算為 `monthly_salary / 30`（與既有 salary engine 對齊）。

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ivy-backend && pytest tests/test_offboarding_step_snapshot_leave.py -v
```

Expected: FAIL — `services.offboarding.steps.snapshot_leave` ImportError。

- [ ] **Step 3: Implement**

```python
# services/offboarding/steps/snapshot_leave.py
"""snapshot_leave step：算特休餘額 + daily_wage 折現，寫 JSONB snapshot。

依賴 utils.leave_quota_helpers.get_annual_leave_balance。
失敗條件：daily_wage 缺失或 0 → 422 LEAVE_BALANCE_NOT_FOUND（折現無法計算）。
"""
from datetime import datetime
from sqlalchemy.orm import Session

from models.employee import Employee
from models.offboarding import EmployeeOffboardingRecord
from services.offboarding.orchestrator import StepResult, OffboardingError
from utils.leave_quota_helpers import get_annual_leave_balance


def _resolve_daily_wage(emp: Employee) -> float | None:
    """取員工日薪。先試直接欄位，否則 monthly_salary / 30。"""
    daily = getattr(emp, "daily_wage", None) or getattr(emp, "daily_salary", None)
    if daily:
        return float(daily)
    monthly = getattr(emp, "monthly_salary", None) or getattr(emp, "base_salary", None)
    if monthly:
        return round(float(monthly) / 30.0, 2)
    return None


def run(session: Session, record: EmployeeOffboardingRecord) -> StepResult:
    emp = session.query(Employee).filter_by(id=record.employee_id).first()
    if emp is None:
        raise OffboardingError(
            f"員工 {record.employee_id} 不存在（snapshot_leave）",
            code="EMPLOYEE_NOT_FOUND",
        )

    daily_wage = _resolve_daily_wage(emp)
    if daily_wage is None or daily_wage == 0:
        raise OffboardingError(
            f"員工 {record.employee_id} 無 daily_wage / monthly_salary，無法折現特休",
            code="LEAVE_BALANCE_NOT_FOUND",
        )

    balance = get_annual_leave_balance(session, emp.id, record.resign_date)
    payout_amount = round(balance["remaining_days"] * daily_wage, 2)

    now = datetime.now()
    record.leave_balance_snapshot = {
        "snapshot_date": balance["snapshot_date"].isoformat(),
        "total_hours": balance["total_hours"],
        "used_hours": balance["used_hours"],
        "remaining_hours": balance["remaining_hours"],
        "remaining_days": balance["remaining_days"],
        "daily_wage": daily_wage,
        "payout_amount": payout_amount,
        "calc_rule_version": "labor_act_38_2026_v1",
    }
    record.leave_snapshot_at = now

    return {
        "step": "snapshot_leave",
        "status": "completed",
        "completed_at": now,
        "payload": {
            "days": balance["remaining_days"],
            "payout": payout_amount,
        },
        "error": None,
    }
```

- [ ] **Step 4: Implement `prefill_salary()` in same module**

snapshot_leave step 3 同時負責 prefill SalaryRecord.unused_leave_payout（spec §5.3 把 step 2+3 都掛在 snapshot_leave 模組）：

在 `services/offboarding/steps/snapshot_leave.py` 末尾加：

```python
def prefill_salary(
    session: Session, record: EmployeeOffboardingRecord
) -> StepResult:
    """step 3 (prefill_leave_payout)：把 snapshot 結果寫入離職當月 SalaryRecord.unused_leave_payout。

    若離職當月 SalaryRecord 不存在則 SKIP（不新建；薪資 calculate 時會新建）；
    存在則覆寫 unused_leave_payout 並標 stale。
    """
    from models.salary import SalaryRecord
    from api.employees import _mark_employee_salary_stale

    snap = record.leave_balance_snapshot
    if not snap:
        return {
            "step": "prefill_leave_payout",
            "status": "skipped",
            "completed_at": datetime.now(),
            "payload": {"reason": "no_snapshot"},
            "error": None,
        }

    target = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == record.employee_id,
            SalaryRecord.salary_year == record.resign_date.year,
            SalaryRecord.salary_month == record.resign_date.month,
        )
        .first()
    )
    if target is None:
        return {
            "step": "prefill_leave_payout",
            "status": "skipped",
            "completed_at": datetime.now(),
            "payload": {"reason": "salary_record_not_yet_created"},
            "error": None,
        }

    target.unused_leave_payout = snap["payout_amount"]
    _mark_employee_salary_stale(session, record.employee_id)

    return {
        "step": "prefill_leave_payout",
        "status": "completed",
        "completed_at": datetime.now(),
        "payload": {
            "salary_record_id": target.id,
            "amount": snap["payout_amount"],
        },
        "error": None,
    }
```

並補測試：

```python
# tests/test_offboarding_step_snapshot_leave.py 補
def test_prefill_salary_writes_to_existing_record(
    db_session, employee_factory, user_factory, leave_quota_factory,
    salary_record_factory,
):
    emp = employee_factory(daily_wage=1800)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    record = _make_record(db_session, emp.id, user.id)
    sr = salary_record_factory(
        employee_id=emp.id, salary_year=2026, salary_month=6
    )

    from services.offboarding.steps.snapshot_leave import run, prefill_salary
    run(db_session, record)
    result = prefill_salary(db_session, record)

    assert result["status"] == "completed"
    assert result["payload"]["salary_record_id"] == sr.id
    db_session.refresh(sr)
    assert float(sr.unused_leave_payout) == 25200.0


def test_prefill_salary_skips_when_no_record(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    emp = employee_factory(daily_wage=1500)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    record = _make_record(db_session, emp.id, user.id)

    from services.offboarding.steps.snapshot_leave import run, prefill_salary
    run(db_session, record)
    result = prefill_salary(db_session, record)

    assert result["status"] == "skipped"
    assert result["payload"]["reason"] == "salary_record_not_yet_created"
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd ivy-backend && pytest tests/test_offboarding_step_snapshot_leave.py -v
```

Expected: PASS 5 tests。

- [ ] **Step 6: Commit**

```bash
cd ivy-backend && git add services/offboarding/steps/snapshot_leave.py \
  tests/test_offboarding_step_snapshot_leave.py && \
git commit -m "feat(offboarding): step snapshot_leave + prefill_leave_payout

run(): 算特休餘額 × daily_wage 寫入 record.leave_balance_snapshot JSONB；
daily_wage 缺失 → 422 LEAVE_BALANCE_NOT_FOUND（折現無法算）。

prefill_salary(): 把 snapshot.payout_amount 寫入離職當月 SalaryRecord
.unused_leave_payout 並標 stale；當月 record 不存在 → skipped（薪資
calculate 時會新建）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Step — revoke_user

**Files:**
- Create: `services/offboarding/steps/revoke_user.py`
- Test: `tests/test_offboarding_step_revoke_user.py`

**動機：** 抽 `api/employees.py:783-806` 既有邏輯成獨立 step；行為一致（已有 `test_employee_offboard_revokes_user.py` 保護）。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_offboarding_step_revoke_user.py
"""驗證 revoke_user step 行為等價於 api/employees.py:783-806。"""
from datetime import date, datetime, timedelta

from services.offboarding.steps.revoke_user import run
from models.offboarding import EmployeeOffboardingRecord


def _make_record(db_session, employee_id, user_id, resign_date):
    record = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=resign_date,
        opened_at=datetime.now(),
        opened_by_user_id=user_id,
    )
    db_session.add(record)
    db_session.flush()
    return record


def test_revokes_when_resign_date_is_today(
    db_session, employee_factory, user_factory
):
    emp = employee_factory()
    admin = user_factory()
    user = user_factory(employee_id=emp.id, is_active=True, token_version=3)
    record = _make_record(db_session, emp.id, admin.id, date.today())

    result = run(db_session, record)
    assert result["step"] == "revoke_user"
    assert result["status"] == "completed"
    assert result["payload"]["username"] == user.username
    db_session.refresh(user)
    assert user.is_active is False
    assert user.token_version == 4
    assert record.user_revoked_at is not None


def test_skips_when_resign_date_future(
    db_session, employee_factory, user_factory
):
    """通知期（resign_date > today）保留 User active 直到當日 cron 自動轉。"""
    emp = employee_factory()
    admin = user_factory()
    user = user_factory(employee_id=emp.id, is_active=True, token_version=3)
    record = _make_record(
        db_session, emp.id, admin.id, date.today() + timedelta(days=14)
    )

    result = run(db_session, record)
    assert result["status"] == "skipped"
    assert result["payload"]["reason"] == "notice_period"
    db_session.refresh(user)
    assert user.is_active is True
    assert user.token_version == 3


def test_completed_with_no_user_when_employee_never_had_account(
    db_session, employee_factory, user_factory
):
    emp = employee_factory()
    admin = user_factory()
    # 不建 user FK
    record = _make_record(db_session, emp.id, admin.id, date.today())

    result = run(db_session, record)
    assert result["status"] == "completed"
    assert result["payload"]["username"] is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ivy-backend && pytest tests/test_offboarding_step_revoke_user.py -v
```

Expected: FAIL — `services.offboarding.steps.revoke_user` ImportError。

- [ ] **Step 3: Implement**

```python
# services/offboarding/steps/revoke_user.py
"""revoke_user step：抽自 api/employees.py:783-806。

resign_date <= today：User.is_active=False + token_version+=1（已簽發 cookie 立刻失效）
resign_date > today：通知期保留 User active；當日 cron 自動轉
"""
import logging
from datetime import date, datetime
from sqlalchemy.orm import Session

from models.auth import User
from models.offboarding import EmployeeOffboardingRecord
from services.offboarding.orchestrator import StepResult

logger = logging.getLogger(__name__)


def run(session: Session, record: EmployeeOffboardingRecord) -> StepResult:
    today = date.today()
    now = datetime.now()

    if record.resign_date > today:
        record.user_revoked_at = None  # 通知期不撤
        return {
            "step": "revoke_user",
            "status": "skipped",
            "completed_at": now,
            "payload": {"reason": "notice_period"},
            "error": None,
        }

    user = (
        session.query(User)
        .filter(
            User.employee_id == record.employee_id,
            User.is_active.is_(True),
        )
        .first()
    )

    if user is None:
        record.user_revoked_at = now
        return {
            "step": "revoke_user",
            "status": "completed",
            "completed_at": now,
            "payload": {"username": None, "note": "no_active_user"},
            "error": None,
        }

    user.is_active = False
    user.token_version = (user.token_version or 0) + 1
    record.user_revoked_at = now

    logger.warning(
        "員工 %s 離職撤 User 帳號：username=%s token_version 升至 %d",
        record.employee_id,
        user.username,
        user.token_version,
    )

    return {
        "step": "revoke_user",
        "status": "completed",
        "completed_at": now,
        "payload": {"username": user.username, "new_token_version": user.token_version},
        "error": None,
    }
```

**注意：** `User` import path 需依 codebase 既有寫法（可能是 `from models.auth import User` 或 `from models.user import User`）。請以 `api/employees.py:792` 既有 import 為準。

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ivy-backend && pytest tests/test_offboarding_step_revoke_user.py -v
```

Expected: PASS 3 tests。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend && git add services/offboarding/steps/revoke_user.py \
  tests/test_offboarding_step_revoke_user.py && \
git commit -m "feat(offboarding): step revoke_user — extract User revoke logic from api/employees.py

抽 api/employees.py:783-806 的 is_active=False + token_version++ 邏輯成獨立
step。行為對齊既有 test_employee_offboard_revokes_user.py。

resign_date > today（通知期）→ skipped（payload reason=notice_period）；
no active user → completed (note=no_active_user)；
正常撤 → completed + payload.new_token_version。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Orchestrator 串接 4 step + 整合測試（含 rollback）

**Files:**
- Modify: `services/offboarding/orchestrator.py`（task 3 殼裡的 `steps_result` 空 list 改成跑 4 step）
- Test: 改 `tests/test_offboarding_orchestrator.py` + 新加 rollback case

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_offboarding_orchestrator.py 改寫 + 加 rollback case
"""驗證 orchestrator 串 4 step + 失敗 rollback。"""
from datetime import date, datetime, timedelta
import pytest

from services.offboarding.orchestrator import (
    process_offboarding,
    OffboardingError,
)
from models.offboarding import EmployeeOffboardingRecord


def test_happy_path_all_4_steps_complete(
    db_session, employee_factory, user_factory, leave_quota_factory,
    salary_record_factory,
):
    emp = employee_factory(daily_wage=1800)
    admin = user_factory()
    user_factory(employee_id=emp.id, is_active=True, token_version=1)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    salary_record_factory(employee_id=emp.id, salary_year=2026, salary_month=6)

    result = process_offboarding(
        session=db_session,
        employee_id=emp.id,
        resign_date=date.today(),
        resign_reason="個人因素",
        operator_user_id=admin.id,
    )
    db_session.commit()

    step_names = [s["step"] for s in result["steps"]]
    assert step_names == [
        "mark_appraisal",
        "snapshot_leave",
        "prefill_leave_payout",
        "revoke_user",
    ]
    assert all(s["status"] == "completed" for s in result["steps"])
    assert result["user_account_revoked"] is True
    assert result["is_active_after"] is False


def test_snapshot_leave_failure_rolls_back_record(
    db_session, employee_factory, user_factory
):
    """員工無 daily_wage → snapshot_leave 422 → record + Employee 寫入全 rollback"""
    emp = employee_factory(daily_wage=None, monthly_salary=None)
    admin = user_factory()

    with pytest.raises(OffboardingError) as exc:
        process_offboarding(
            session=db_session,
            employee_id=emp.id,
            resign_date=date.today(),
            resign_reason="test",
            operator_user_id=admin.id,
        )
    db_session.rollback()
    assert exc.value.code == "LEAVE_BALANCE_NOT_FOUND"

    # rollback 後 record 不存在
    assert db_session.query(EmployeeOffboardingRecord).filter_by(
        employee_id=emp.id
    ).first() is None
    db_session.refresh(emp)
    assert emp.resign_date is None
    assert emp.is_active is True


def test_duplicate_offboarding_raises_already_offboarded(
    db_session, employee_factory, user_factory, leave_quota_factory,
):
    emp = employee_factory(daily_wage=1800)
    admin = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    process_offboarding(
        session=db_session,
        employee_id=emp.id,
        resign_date=date.today(),
        resign_reason="first",
        operator_user_id=admin.id,
    )
    db_session.commit()

    with pytest.raises(OffboardingError) as exc:
        process_offboarding(
            session=db_session,
            employee_id=emp.id,
            resign_date=date.today() + timedelta(days=1),
            resign_reason="second",
            operator_user_id=admin.id,
        )
    assert exc.value.code == "ALREADY_OFFBOARDED"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ivy-backend && pytest tests/test_offboarding_orchestrator.py -v
```

Expected: FAIL — orchestrator 內 steps_result 仍空。

- [ ] **Step 3: Modify orchestrator to run 4 steps**

在 `services/offboarding/orchestrator.py` 把 task 3 留的 `steps_result: list[StepResult] = []` 區塊改為：

```python
    from services.offboarding.steps import (
        mark_appraisal,
        snapshot_leave,
        revoke_user,
    )

    steps_result: list[StepResult] = []
    user_account_revoked = False

    try:
        # Step 1: mark_appraisal
        steps_result.append(mark_appraisal.run(session, record))

        # Step 2: snapshot_leave
        steps_result.append(snapshot_leave.run(session, record))

        # Step 3: prefill_leave_payout（同模組 prefill_salary）
        steps_result.append(snapshot_leave.prefill_salary(session, record))

        # Step 4: revoke_user
        revoke_result = revoke_user.run(session, record)
        steps_result.append(revoke_result)
        if revoke_result["status"] == "completed" and revoke_result["payload"].get("username"):
            user_account_revoked = True

    except OffboardingError:
        raise  # 由 endpoint 層 catch + session.rollback

    return OffboardingResult(
        employee_id=employee_id,
        resign_date=resign_date,
        is_active_after=emp.is_active,
        user_account_revoked=user_account_revoked,
        steps=steps_result,
        certificate_pdf_path=None,
    )
```

並在 file 頂 import 區補：

```python
from services.offboarding.orchestrator import StepResult  # 不必，types 已在本檔
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ivy-backend && pytest tests/test_offboarding_orchestrator.py tests/test_offboarding_step_*.py -v
```

Expected: PASS 全部。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend && git add services/offboarding/orchestrator.py \
  tests/test_offboarding_orchestrator.py && \
git commit -m "feat(offboarding): orchestrator wires 4 steps + rollback on failure

固定順序：mark_appraisal → snapshot_leave → prefill_leave_payout → revoke_user。
任一 step raise OffboardingError → 整筆 transaction 由呼叫端 rollback；DB 一致。

新測試覆蓋：happy path 4 step completed、snapshot_leave 失敗 rollback record
+ Employee.resign_date 復原、ALREADY_OFFBOARDED 阻擋重複呼叫。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Schemas + Router 殼 + main.py 註冊

**Files:**
- Create: `schemas/offboarding.py`
- Create: `api/offboarding.py`（router 殼，無 endpoint 內容）
- Modify: `main.py`（註冊 router）

- [ ] **Step 1: Write the failing test (smoke)**

```python
# tests/test_offboarding_api.py
"""驗證 router 註冊 + endpoint path 存在（後續 task 補完整 case）。"""
def test_router_registered(client):
    response = client.post("/api/offboarding/999/preview", json={
        "resign_date": "2026-06-15"
    })
    # 應該回 401 (no auth) 或 404 (employee not found) — 兩者皆代表 router 存在
    assert response.status_code in (401, 404, 422)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ivy-backend && pytest tests/test_offboarding_api.py -v
```

Expected: FAIL — 404 path not found（router 未註冊）。

- [ ] **Step 3: Create schemas**

```python
# schemas/offboarding.py
"""Pydantic schema for offboarding endpoints."""
from datetime import date, datetime
from pydantic import BaseModel, Field
from typing import Optional, Literal


class OffboardingPreviewRequest(BaseModel):
    resign_date: date
    resign_reason: Optional[str] = None


class OffboardingProcessRequest(BaseModel):
    resign_date: date
    resign_reason: Optional[str] = None


class LeaveSnapshotPreview(BaseModel):
    special_leave_days: float
    daily_wage: float
    payout_amount: float


class SalaryRecordTarget(BaseModel):
    year: int
    month: int
    exists: bool
    will_be_marked_stale: bool


class AppraisalInFlightCycle(BaseModel):
    cycle_id: int
    cycle_name: str
    current_score: Optional[float] = None


class OffboardingPreview(BaseModel):
    user_account_will_be_revoked: bool
    leave_snapshot: LeaveSnapshotPreview
    salary_record_target: SalaryRecordTarget
    appraisal_in_flight_cycles: list[AppraisalInFlightCycle]
    certificate_pdf_ready_to_generate: bool


class OffboardingPreviewResponse(BaseModel):
    employee_id: int
    employee_name: str
    resign_date: date
    preview: OffboardingPreview
    warnings: list[str] = Field(default_factory=list)


class StepResultModel(BaseModel):
    step: str
    status: Literal["completed", "skipped", "failed"]
    completed_at: Optional[datetime] = None
    payload: Optional[dict] = None
    error: Optional[str] = None


class OffboardingProcessResponse(BaseModel):
    employee_id: int
    resign_date: date
    is_active: bool
    user_account_revoked: bool
    steps: list[StepResultModel]
    certificate_download_url: Optional[str] = None  # Phase 1 一律 None


class OffboardingDetailResponse(BaseModel):
    employee_id: int
    employee_name: str
    resign_date: date
    resign_reason: Optional[str]
    opened_at: datetime
    opened_by_user_id: int
    appraisal_marked_at: Optional[datetime]
    leave_snapshot_at: Optional[datetime]
    user_revoked_at: Optional[datetime]
    certificate_generated_at: Optional[datetime]
    leave_balance_snapshot: Optional[dict]
    certificate_pdf_path: Optional[str]
    nhi_unenroll_submitted_at: Optional[datetime]
    magic_link_active: bool  # 派生：token_hash & not revoked & not expired & count<3
    closed_at: Optional[datetime]


class NhiUnenrollRequest(BaseModel):
    submitted: bool
```

- [ ] **Step 4: Create router shell**

```python
# api/offboarding.py
"""員工離職 checklist API endpoint（Phase 1）。

Phase 1 提供：preview / process / get / nhi-unenroll
Phase 2 補：certificate.pdf / magic-link / download
Phase 3 補：list
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from models.database import get_session
from utils.permissions import Permission
from utils.auth import require_staff_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/offboarding", tags=["offboarding"])


# endpoints 於後續 task 加入
```

- [ ] **Step 5: Register router in `main.py`**

找到既有 router include block（grep `include_router(employees`），在其後加：

```python
from api.offboarding import router as offboarding_router
app.include_router(offboarding_router, prefix="/api")
```

- [ ] **Step 6: Run test to verify it passes**

```bash
cd ivy-backend && pytest tests/test_offboarding_api.py::test_router_registered -v
```

Expected: PASS（status_code 401 或 422 都算通過）。

- [ ] **Step 7: Commit**

```bash
cd ivy-backend && git add schemas/offboarding.py api/offboarding.py main.py \
  tests/test_offboarding_api.py && \
git commit -m "feat(offboarding): router shell + Pydantic schemas + main.py registration

新 router api/offboarding.py 註冊於 /api/offboarding（tags=[offboarding]）。
Pydantic schema 涵蓋 preview/process/detail/nhi-unenroll request/response shapes。
endpoint 實作於後續 task 加入。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Endpoint `POST /offboarding/{id}/preview`

**Files:**
- Modify: `api/offboarding.py`
- Test: `tests/test_offboarding_api.py`（補 preview case）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_offboarding_api.py 補
def test_preview_returns_leave_snapshot_and_warnings(
    client, admin_login, employee_factory, leave_quota_factory,
    salary_record_factory,
):
    emp = employee_factory(name="王小明", daily_wage=1800)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    salary_record_factory(employee_id=emp.id, salary_year=2026, salary_month=6)

    headers = admin_login()
    response = client.post(
        f"/api/offboarding/{emp.id}/preview",
        json={"resign_date": "2026-06-15", "resign_reason": "test"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["employee_id"] == emp.id
    assert body["employee_name"] == "王小明"
    assert body["preview"]["leave_snapshot"]["special_leave_days"] == 14.0
    assert body["preview"]["leave_snapshot"]["payout_amount"] == 25200
    assert body["preview"]["salary_record_target"]["exists"] is True


def test_preview_returns_404_for_unknown_employee(client, admin_login):
    headers = admin_login()
    response = client.post(
        "/api/offboarding/99999/preview",
        json={"resign_date": "2026-06-15"},
        headers=headers,
    )
    assert response.status_code == 404


def test_preview_does_not_write_to_db(
    client, admin_login, db_session, employee_factory, leave_quota_factory,
):
    emp = employee_factory(daily_wage=1800)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    headers = admin_login()
    client.post(
        f"/api/offboarding/{emp.id}/preview",
        json={"resign_date": "2026-06-15"},
        headers=headers,
    )
    db_session.refresh(emp)
    assert emp.resign_date is None  # 純讀
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ivy-backend && pytest tests/test_offboarding_api.py::test_preview_returns_leave_snapshot_and_warnings -v
```

Expected: FAIL — endpoint 不存在。

- [ ] **Step 3: Implement endpoint**

在 `api/offboarding.py` 加：

```python
from datetime import date

from schemas.offboarding import (
    OffboardingPreviewRequest, OffboardingPreviewResponse,
    OffboardingPreview, LeaveSnapshotPreview, SalaryRecordTarget,
    AppraisalInFlightCycle,
)
from models.employee import Employee
from models.salary import SalaryRecord
from models.auth import User
from utils.leave_quota_helpers import get_annual_leave_balance


@router.post("/{employee_id}/preview", response_model=OffboardingPreviewResponse)
def preview_offboarding(
    employee_id: int,
    req: OffboardingPreviewRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    """預覽離職將執行的動作（純讀，不寫 DB）。"""
    session: Session = get_session()
    try:
        emp = session.query(Employee).filter_by(id=employee_id).first()
        if emp is None:
            raise HTTPException(status_code=404, detail="EMPLOYEE_NOT_FOUND")

        balance = get_annual_leave_balance(session, employee_id, req.resign_date)
        daily_wage = (
            getattr(emp, "daily_wage", None)
            or getattr(emp, "daily_salary", None)
            or (
                round(float(getattr(emp, "monthly_salary", 0) or 0) / 30.0, 2)
            )
        )
        payout = round(balance["remaining_days"] * (daily_wage or 0), 2)

        sr = (
            session.query(SalaryRecord)
            .filter(
                SalaryRecord.employee_id == employee_id,
                SalaryRecord.salary_year == req.resign_date.year,
                SalaryRecord.salary_month == req.resign_date.month,
            )
            .first()
        )

        today = date.today()
        user_active = (
            session.query(User)
            .filter(
                User.employee_id == employee_id,
                User.is_active.is_(True),
            )
            .first()
        )

        # appraisal in-flight：跨檔查 cycle；Phase 1 簡化用空 list
        # （task 14 aggregator filter 改後自動含；preview 顯示僅作 hint）
        in_flight_cycles: list[AppraisalInFlightCycle] = []

        warnings: list[str] = []
        if in_flight_cycles:
            warnings.append(
                f"員工有 {len(in_flight_cycles)} 個進行中考核 cycle，"
                "標旗後仍保留於評議名單需 admin 人工結算"
            )
        if not daily_wage:
            warnings.append("員工無 daily_wage / monthly_salary，特休折現無法計算")

        return OffboardingPreviewResponse(
            employee_id=employee_id,
            employee_name=emp.name,
            resign_date=req.resign_date,
            preview=OffboardingPreview(
                user_account_will_be_revoked=(req.resign_date <= today and user_active is not None),
                leave_snapshot=LeaveSnapshotPreview(
                    special_leave_days=balance["remaining_days"],
                    daily_wage=float(daily_wage or 0),
                    payout_amount=payout,
                ),
                salary_record_target=SalaryRecordTarget(
                    year=req.resign_date.year,
                    month=req.resign_date.month,
                    exists=sr is not None,
                    will_be_marked_stale=sr is not None,
                ),
                appraisal_in_flight_cycles=in_flight_cycles,
                certificate_pdf_ready_to_generate=False,  # Phase 2 才實作
            ),
            warnings=warnings,
        )
    finally:
        session.close()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ivy-backend && pytest tests/test_offboarding_api.py -v
```

Expected: PASS 4 tests（含 task 8 smoke）。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend && git add api/offboarding.py tests/test_offboarding_api.py && \
git commit -m "feat(offboarding): POST /offboarding/{id}/preview endpoint

純讀 endpoint：算特休 snapshot、找 salary record、查 active User，
回 OffboardingPreviewResponse 含 warnings list 讓前端紅字提醒。
EMPLOYEES_WRITE permission 守衛（preview 是 admin 流程的一部分）。

Phase 1 appraisal_in_flight_cycles 暫回空 list；aggregator filter 改後
（task 14）dashboard 即會自然含當期離職員工。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Endpoint `POST /offboarding/{id}/process`

**Files:**
- Modify: `api/offboarding.py`
- Test: `tests/test_offboarding_api.py`（補 process case）

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_offboarding_api.py 補
def test_process_happy_path(
    client, admin_login, db_session, employee_factory, user_factory,
    leave_quota_factory, salary_record_factory,
):
    emp = employee_factory(daily_wage=1800)
    user_factory(employee_id=emp.id, is_active=True, token_version=1)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    sr = salary_record_factory(
        employee_id=emp.id, salary_year=2026, salary_month=6
    )

    headers = admin_login()
    response = client.post(
        f"/api/offboarding/{emp.id}/process",
        json={"resign_date": "2026-06-15", "resign_reason": "個人因素"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_active"] is False
    assert body["user_account_revoked"] is True
    steps = [s["step"] for s in body["steps"]]
    assert "mark_appraisal" in steps
    assert "snapshot_leave" in steps
    assert "revoke_user" in steps

    db_session.refresh(sr)
    assert float(sr.unused_leave_payout) == 25200.0


def test_process_already_offboarded_returns_409(
    client, admin_login, employee_factory, leave_quota_factory,
):
    emp = employee_factory(daily_wage=1800)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    headers = admin_login()
    # 第一次成功
    r1 = client.post(
        f"/api/offboarding/{emp.id}/process",
        json={"resign_date": "2026-06-15"},
        headers=headers,
    )
    assert r1.status_code == 200
    # 第二次 409
    r2 = client.post(
        f"/api/offboarding/{emp.id}/process",
        json={"resign_date": "2026-07-01"},
        headers=headers,
    )
    assert r2.status_code == 409
    assert r2.json()["detail"] == "ALREADY_OFFBOARDED"


def test_process_resign_before_hire_returns_400(
    client, admin_login, employee_factory,
):
    from datetime import date
    emp = employee_factory(daily_wage=1800, hire_date=date(2025, 12, 1))
    headers = admin_login()
    response = client.post(
        f"/api/offboarding/{emp.id}/process",
        json={"resign_date": "2025-11-15"},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "RESIGN_DATE_BEFORE_HIRE"


def test_process_failure_rolls_back_all_changes(
    client, admin_login, db_session, employee_factory,
):
    """員工無 daily_wage / monthly_salary → 422，Employee/Record 全 rollback"""
    emp = employee_factory(daily_wage=None, monthly_salary=None)
    headers = admin_login()
    response = client.post(
        f"/api/offboarding/{emp.id}/process",
        json={"resign_date": "2026-06-15"},
        headers=headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "LEAVE_BALANCE_NOT_FOUND"

    db_session.refresh(emp)
    assert emp.resign_date is None
    assert emp.is_active is True

    from models.offboarding import EmployeeOffboardingRecord
    assert db_session.query(EmployeeOffboardingRecord).filter_by(
        employee_id=emp.id
    ).first() is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ivy-backend && pytest tests/test_offboarding_api.py::test_process_happy_path -v
```

Expected: FAIL — endpoint 不存在。

- [ ] **Step 3: Implement endpoint**

在 `api/offboarding.py` 加：

```python
from schemas.offboarding import (
    OffboardingProcessRequest, OffboardingProcessResponse, StepResultModel,
)
from services.offboarding.orchestrator import (
    process_offboarding, OffboardingError,
)


_ERROR_TO_STATUS = {
    "EMPLOYEE_NOT_FOUND": 404,
    "ALREADY_OFFBOARDED": 409,
    "RESIGN_DATE_BEFORE_HIRE": 400,
    "RESIGN_DATE_TOO_FAR_FUTURE": 400,
    "LEAVE_BALANCE_NOT_FOUND": 422,
    "CERTIFICATE_GENERATION_FAILED": 500,
}


@router.post("/{employee_id}/process", response_model=OffboardingProcessResponse)
def process_offboarding_endpoint(
    employee_id: int,
    req: OffboardingProcessRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    """一鍵離職主處理 endpoint。"""
    session: Session = get_session()
    try:
        operator_user_id = int(current_user.get("user_id") or current_user.get("sub"))
        try:
            result = process_offboarding(
                session=session,
                employee_id=employee_id,
                resign_date=req.resign_date,
                resign_reason=req.resign_reason,
                operator_user_id=operator_user_id,
            )
        except OffboardingError as e:
            session.rollback()
            status = _ERROR_TO_STATUS.get(e.code, 500)
            raise HTTPException(status_code=status, detail=e.code)

        session.commit()

        # audit log（既有 middleware pattern：在 request.state 記）
        request.state.audit_action = "OFFBOARDING_PROCESSED"
        request.state.audit_target = f"employee/{employee_id}"
        request.state.audit_meta = {
            "resign_date": str(result["resign_date"]),
            "steps_completed": [s["step"] for s in result["steps"]
                                if s["status"] == "completed"],
        }

        logger.warning(
            "離職處理完成：employee_id=%s resign_date=%s operator=%s",
            employee_id, result["resign_date"], current_user.get("username"),
        )

        return OffboardingProcessResponse(
            employee_id=employee_id,
            resign_date=result["resign_date"],
            is_active=result["is_active_after"],
            user_account_revoked=result["user_account_revoked"],
            steps=[StepResultModel(**s) for s in result["steps"]],
            certificate_download_url=None,  # Phase 2 才補
        )
    finally:
        session.close()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ivy-backend && pytest tests/test_offboarding_api.py -v
```

Expected: PASS 全部 process case。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend && git add api/offboarding.py tests/test_offboarding_api.py && \
git commit -m "feat(offboarding): POST /offboarding/{id}/process endpoint

主處理 endpoint：呼叫 orchestrator → 失敗 rollback + raise HTTPException
（_ERROR_TO_STATUS 映射 OffboardingError.code 到 4xx/5xx）。

正常路徑寫 audit log OFFBOARDING_PROCESSED；session.commit() 由 endpoint
負責，orchestrator 不 commit。

EMPLOYEES_WRITE permission 守衛。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Endpoint `GET /offboarding/{id}` + `PATCH /nhi-unenroll`

**Files:**
- Modify: `api/offboarding.py`
- Test: `tests/test_offboarding_api.py` 補 case

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_offboarding_api.py 補
def test_get_detail_returns_full_record(
    client, admin_login, employee_factory, user_factory, leave_quota_factory,
):
    emp = employee_factory(name="李四", daily_wage=1500)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    headers = admin_login()
    client.post(
        f"/api/offboarding/{emp.id}/process",
        json={"resign_date": "2026-06-15", "resign_reason": "另謀高就"},
        headers=headers,
    )

    response = client.get(f"/api/offboarding/{emp.id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["employee_id"] == emp.id
    assert body["employee_name"] == "李四"
    assert body["resign_reason"] == "另謀高就"
    assert body["leave_snapshot_at"] is not None
    assert body["leave_balance_snapshot"]["remaining_days"] == 10.0
    assert body["magic_link_active"] is False  # Phase 1 未產 token


def test_get_detail_404_for_employee_without_record(
    client, admin_login, employee_factory,
):
    emp = employee_factory()
    headers = admin_login()
    response = client.get(f"/api/offboarding/{emp.id}", headers=headers)
    assert response.status_code == 404


def test_patch_nhi_unenroll_sets_timestamp(
    client, admin_login, employee_factory, leave_quota_factory,
):
    emp = employee_factory(daily_wage=1500)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    headers = admin_login()
    client.post(
        f"/api/offboarding/{emp.id}/process",
        json={"resign_date": "2026-06-15"},
        headers=headers,
    )

    r1 = client.patch(
        f"/api/offboarding/{emp.id}/nhi-unenroll",
        json={"submitted": True},
        headers=headers,
    )
    assert r1.status_code == 200

    r2 = client.get(f"/api/offboarding/{emp.id}", headers=headers)
    assert r2.json()["nhi_unenroll_submitted_at"] is not None

    # 取消
    r3 = client.patch(
        f"/api/offboarding/{emp.id}/nhi-unenroll",
        json={"submitted": False},
        headers=headers,
    )
    assert r3.status_code == 200
    r4 = client.get(f"/api/offboarding/{emp.id}", headers=headers)
    assert r4.json()["nhi_unenroll_submitted_at"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ivy-backend && pytest tests/test_offboarding_api.py -k "detail or nhi_unenroll" -v
```

Expected: FAIL — endpoint 不存在。

- [ ] **Step 3: Implement endpoints**

在 `api/offboarding.py` 加：

```python
from datetime import datetime, timedelta
from schemas.offboarding import OffboardingDetailResponse, NhiUnenrollRequest
from models.offboarding import EmployeeOffboardingRecord


def _is_magic_link_active(record: EmployeeOffboardingRecord) -> bool:
    if not record.magic_link_token_hash:
        return False
    if record.magic_link_revoked_at:
        return False
    if record.magic_link_expires_at and record.magic_link_expires_at < datetime.now():
        return False
    if (record.magic_link_download_count or 0) >= 3:
        return False
    return True


@router.get("/{employee_id}", response_model=OffboardingDetailResponse)
def get_offboarding_detail(
    employee_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_READ)),
):
    session: Session = get_session()
    try:
        record = (
            session.query(EmployeeOffboardingRecord)
            .filter_by(employee_id=employee_id)
            .first()
        )
        if record is None:
            raise HTTPException(status_code=404, detail="OFFBOARDING_RECORD_NOT_FOUND")

        emp = session.query(Employee).filter_by(id=employee_id).first()
        return OffboardingDetailResponse(
            employee_id=record.employee_id,
            employee_name=emp.name if emp else "",
            resign_date=record.resign_date,
            resign_reason=record.resign_reason,
            opened_at=record.opened_at,
            opened_by_user_id=record.opened_by_user_id,
            appraisal_marked_at=record.appraisal_marked_at,
            leave_snapshot_at=record.leave_snapshot_at,
            user_revoked_at=record.user_revoked_at,
            certificate_generated_at=record.certificate_generated_at,
            leave_balance_snapshot=record.leave_balance_snapshot,
            certificate_pdf_path=record.certificate_pdf_path,
            nhi_unenroll_submitted_at=record.nhi_unenroll_submitted_at,
            magic_link_active=_is_magic_link_active(record),
            closed_at=record.closed_at,
        )
    finally:
        session.close()


@router.patch("/{employee_id}/nhi-unenroll")
def patch_nhi_unenroll(
    employee_id: int,
    req: NhiUnenrollRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    session: Session = get_session()
    try:
        record = (
            session.query(EmployeeOffboardingRecord)
            .filter_by(employee_id=employee_id)
            .first()
        )
        if record is None:
            raise HTTPException(status_code=404, detail="OFFBOARDING_RECORD_NOT_FOUND")

        record.nhi_unenroll_submitted_at = datetime.now() if req.submitted else None
        session.commit()

        request.state.audit_action = "OFFBOARDING_NHI_FLAG_UPDATED"
        request.state.audit_target = f"employee/{employee_id}"
        request.state.audit_meta = {"submitted": req.submitted}

        return {"employee_id": employee_id, "nhi_unenroll_submitted_at": record.nhi_unenroll_submitted_at}
    finally:
        session.close()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ivy-backend && pytest tests/test_offboarding_api.py -v
```

Expected: PASS 全部。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend && git add api/offboarding.py tests/test_offboarding_api.py && \
git commit -m "feat(offboarding): GET /{id} + PATCH /{id}/nhi-unenroll endpoints

GET /{id}：回完整 EmployeeOffboardingRecord 序列化；magic_link_active 派生
（hash 存 + not revoked + not expired + count<3）；EMPLOYEES_READ。

PATCH /{id}/nhi-unenroll：admin 手動勾選 nhi_unenroll_submitted_at；
可設 null（取消）；audit OFFBOARDING_NHI_FLAG_UPDATED；EMPLOYEES_WRITE。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Aggregator filter 改條件（救火 dashboard）

**Files:**
- Modify: `services/appraisal/status_aggregator.py:451-465`
- Test: `tests/test_appraisal_aggregator_offboarding.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_appraisal_aggregator_offboarding.py
"""驗證 aggregator filter 改後納入當期離職員工。"""
from datetime import date

from services.appraisal.status_aggregator import (
    aggregate_all_active_employees_status,
)


def test_includes_employee_who_resigned_within_cycle(
    db_session, employee_factory, appraisal_cycle_factory,
):
    cycle = appraisal_cycle_factory(
        start_date=date(2026, 5, 1), end_date=date(2026, 6, 30)
    )
    active_emp = employee_factory(is_active=True)
    mid_cycle_resignee = employee_factory(
        is_active=False, resign_date=date(2026, 6, 10)  # 在 cycle 內
    )
    pre_cycle_resignee = employee_factory(
        is_active=False, resign_date=date(2026, 3, 15)  # cycle 前已離
    )

    result = aggregate_all_active_employees_status(db_session, cycle.id)
    emp_ids = {row["employee_id"] for row in result}

    assert active_emp.id in emp_ids
    assert mid_cycle_resignee.id in emp_ids  # 新行為：cycle 內離職納入
    assert pre_cycle_resignee.id not in emp_ids  # cycle 前離職不納入


def test_existing_active_employees_still_included(
    db_session, employee_factory, appraisal_cycle_factory,
):
    """回歸：原本 is_active=True 員工繼續出現"""
    cycle = appraisal_cycle_factory(
        start_date=date(2026, 5, 1), end_date=date(2026, 6, 30)
    )
    e1 = employee_factory(is_active=True)
    e2 = employee_factory(is_active=True)
    result = aggregate_all_active_employees_status(db_session, cycle.id)
    emp_ids = {row["employee_id"] for row in result}
    assert e1.id in emp_ids
    assert e2.id in emp_ids
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ivy-backend && pytest tests/test_appraisal_aggregator_offboarding.py -v
```

Expected: FAIL — `mid_cycle_resignee` 不在結果中（filter 仍寫死 `is_active=True`）。

- [ ] **Step 3: Modify filter**

```bash
cd ivy-backend && grep -n "is_active == True" services/appraisal/status_aggregator.py
```

預期看到 line 463（或附近）有：
```python
session.query(Employee).filter(Employee.is_active == True).all()  # noqa: E712
```

修改 — 需先取 cycle 物件以取得 `cycle.start_date` / `cycle.end_date`。

```python
# services/appraisal/status_aggregator.py
# 在 function 開頭已有 cycle 取得邏輯；找該 query 改為：

from sqlalchemy import or_, and_

# ...

employees = (
    session.query(Employee)
    .filter(
        or_(
            Employee.is_active == True,  # noqa: E712
            and_(
                Employee.resign_date.isnot(None),
                Employee.resign_date >= cycle.start_date,
                Employee.resign_date <= cycle.end_date,
            ),
        )
    )
    .all()
)
```

**注意：** 確認 line 451-465 的 function 已有 `cycle` 物件可用；若無需要先 `cycle = session.query(AppraisalCycle).filter_by(id=cycle_id).first()`。

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ivy-backend && pytest tests/test_appraisal_aggregator_offboarding.py tests/test_appraisal_aggregator.py -v
```

Expected: PASS 新 test + 舊 test 不回歸。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend && git add services/appraisal/status_aggregator.py \
  tests/test_appraisal_aggregator_offboarding.py && \
git commit -m "fix(appraisal): aggregator filter includes employees resigned within cycle

舊：filter(Employee.is_active == True) — 離職員工立刻從 dashboard 消失，
in-flight 考核無法評議（spec §1 P0 缺口）。

新：or_(is_active=True, (resign_date BETWEEN cycle.start AND cycle.end))
— 當期離職員工繼續出現於 dashboard，admin 可人工評議；cycle 前已離者不含。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: 舊 `/employees/{id}/resign` → deprecation passthrough

**Files:**
- Modify: `api/employees.py:759-824`（既有 resign endpoint）
- Test: `tests/test_offboarding_legacy_endpoint_passthrough.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_offboarding_legacy_endpoint_passthrough.py
"""驗證舊 /employees/{id}/resign 改為 passthrough 呼叫 orchestrator。"""
def test_legacy_resign_endpoint_creates_offboarding_record(
    client, admin_login, db_session, employee_factory, leave_quota_factory,
):
    emp = employee_factory(daily_wage=1800)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    headers = admin_login()

    response = client.put(
        f"/api/employees/{emp.id}/resign",
        json={"resign_date": "2026-06-15", "resign_reason": "test"},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    # 既有測試覆蓋：Employee.is_active=False、token revoke
    db_session.refresh(emp)
    assert emp.is_active is False

    # 新行為：建 EmployeeOffboardingRecord
    from models.offboarding import EmployeeOffboardingRecord
    record = db_session.query(EmployeeOffboardingRecord).filter_by(
        employee_id=emp.id
    ).first()
    assert record is not None
    assert record.resign_date.isoformat() == "2026-06-15"
    assert record.leave_balance_snapshot is not None  # snapshot_leave step 跑過
```

**注意：** 既有 `tests/test_employee_offboard_revokes_user.py` 必須繼續通過 — 此 task 行為向後相容。

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ivy-backend && pytest tests/test_offboarding_legacy_endpoint_passthrough.py -v
```

Expected: FAIL — record 未建立（舊 endpoint 不會走 orchestrator）。

- [ ] **Step 3: Modify legacy endpoint**

打開 `api/employees.py:759-824`，找到 `def offboard_employee(...)` function body，**整段替換** 為 passthrough：

```python
@router.put("/employees/{employee_id}/resign")
def offboard_employee(
    employee_id: int,
    req: ResignRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.EMPLOYEES_WRITE)),
):
    """[DEPRECATED] 改用 POST /api/offboarding/{id}/process。

    本 endpoint 保留作 deprecation passthrough，前端切完後（Phase 3）移除。
    """
    from services.offboarding.orchestrator import (
        process_offboarding, OffboardingError,
    )
    from datetime import date
    from sqlalchemy.orm import Session

    resign_d = req.resign_date if isinstance(req.resign_date, date) else date.fromisoformat(req.resign_date)
    session = get_session()
    try:
        operator_user_id = int(current_user.get("user_id") or current_user.get("sub"))
        try:
            result = process_offboarding(
                session=session,
                employee_id=employee_id,
                resign_date=resign_d,
                resign_reason=req.resign_reason,
                operator_user_id=operator_user_id,
            )
        except OffboardingError as e:
            session.rollback()
            status_map = {
                "EMPLOYEE_NOT_FOUND": 404,
                "ALREADY_OFFBOARDED": 409,
                "RESIGN_DATE_BEFORE_HIRE": 400,
                "RESIGN_DATE_TOO_FAR_FUTURE": 400,
                "LEAVE_BALANCE_NOT_FOUND": 422,
            }
            raise HTTPException(status_code=status_map.get(e.code, 500), detail=e.code)

        session.commit()
        # 向後相容回傳 shape（前端尚未切換期間）
        return {
            "message": "離職資料已更新（deprecated; use POST /offboarding/{id}/process）",
            "id": employee_id,
            "name": (session.query(Employee).filter_by(id=employee_id).first().name),
            "resign_date": result["resign_date"].isoformat(),
            "resign_reason": req.resign_reason,
            "is_active": result["is_active_after"],
            "user_account_revoked": result["user_account_revoked"],
        }
    finally:
        session.close()
```

**刪除** 舊 function body 內所有 `_mark_employee_salary_stale(...)`、User revoke、Employee 寫入等邏輯（已被 orchestrator 覆蓋）。

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ivy-backend && pytest tests/test_offboarding_legacy_endpoint_passthrough.py \
  tests/test_employee_offboard_revokes_user.py -v
```

Expected: PASS 兩組 test。

- [ ] **Step 5: Commit**

```bash
cd ivy-backend && git add api/employees.py tests/test_offboarding_legacy_endpoint_passthrough.py && \
git commit -m "refactor(employees): legacy /resign endpoint → orchestrator passthrough

舊 PUT /employees/{id}/resign 改呼叫 services/offboarding/orchestrator.
process_offboarding，行為包含原 User revoke + Employee.is_active + 新增
EmployeeOffboardingRecord + leave snapshot + appraisal mark。

response shape 保持向後相容（前端尚未切到 /offboarding/{id}/process），
Phase 3 前端切完 PR 移除此 endpoint。

既有 test_employee_offboard_revokes_user.py 持續通過（行為相容）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: Sentry PII denylist 同步

**Files:**
- Modify: `utils/sentry_init.py`（找 `_PII_KEY_SUBSTRINGS` set 加 3 個）
- Test: 既有 `tests/test_sentry_pii_scrub.py` 補（若不存在則新建簡短驗證）

- [ ] **Step 1: Check existing Sentry test file**

```bash
cd ivy-backend && ls tests/ | grep -i sentry
```

如有 `test_sentry_*.py` 則補 case；否則新建。

- [ ] **Step 2: Write the failing test**

```python
# tests/test_sentry_pii_offboarding.py
"""驗證 offboarding 新 PII 欄位被 denylist 涵蓋。"""
from utils.sentry_init import _scrub_event


def test_resign_reason_is_scrubbed():
    event = {"extra": {"resign_reason": "個人因素"}}
    scrubbed = _scrub_event(event, None)
    assert scrubbed["extra"]["resign_reason"] != "個人因素"


def test_leave_balance_snapshot_is_scrubbed():
    event = {"extra": {"leave_balance_snapshot": {"daily_wage": 1800}}}
    scrubbed = _scrub_event(event, None)
    assert scrubbed["extra"]["leave_balance_snapshot"] != {"daily_wage": 1800}


def test_certificate_pdf_path_is_scrubbed():
    event = {"extra": {"certificate_pdf_path": "storage/offboarding/42_2026-06-15.pdf"}}
    scrubbed = _scrub_event(event, None)
    assert "42_2026-06-15.pdf" not in str(scrubbed)
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd ivy-backend && pytest tests/test_sentry_pii_offboarding.py -v
```

Expected: FAIL — 3 key 未在 denylist。

- [ ] **Step 4: Add to denylist**

```bash
cd ivy-backend && grep -n "_PII_KEY_SUBSTRINGS" utils/sentry_init.py
```

在 `_PII_KEY_SUBSTRINGS` 內加：

```python
_PII_KEY_SUBSTRINGS = frozenset({
    # ... existing entries
    "resign_reason",
    "leave_balance_snapshot",
    "certificate_pdf_path",
})
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd ivy-backend && pytest tests/test_sentry_pii_offboarding.py -v
```

Expected: PASS。

- [ ] **Step 6: Sync frontend (workspace-level reminder, not a code change in this task)**

加 TODO 到 commit message 提醒：前端 `ivy-frontend/src/utils/sentry.js` `PII_KEY_SUBSTRINGS` 同步同 3 key — 此 task 不跨 repo，由 Phase 3 前端 PR 處理。

- [ ] **Step 7: Commit**

```bash
cd ivy-backend && git add utils/sentry_init.py tests/test_sentry_pii_offboarding.py && \
git commit -m "feat(sentry): add offboarding PII keys to denylist

新增 3 key：resign_reason / leave_balance_snapshot / certificate_pdf_path
（certificate_pdf_path 雖為檔路徑非個資，但路徑含員工姓名 → 一併加入）。

TODO: 前端 src/utils/sentry.js PII_KEY_SUBSTRINGS 同步加同 3 key（Phase 3 PR）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 15: OpenAPI dump + 前端 schema regen + 全 suite 驗證

**Files:**
- Run: `python scripts/dump_openapi.py`（不入 repo）
- Run: `cd ../ivy-frontend && npm run gen:api`（產 schema.d.ts）

- [ ] **Step 1: Run full pytest one final time**

```bash
cd ivy-backend && pytest --ignore=tests/test_audit_router.py \
  --ignore=tests/test_supabase_storage.py 2>&1 | tail -10
```

Expected: PASS（4319+ 既有 + 約 30 個新 Phase 1 case）。記下總數寫入 commit message。

- [ ] **Step 2: Dump OpenAPI**

```bash
cd ivy-backend && python scripts/dump_openapi.py
```

確認 `openapi.json` 含 `/offboarding/*` 路徑（用 grep 或 python 解析）。檔案 `.gitignore` 擋住不入 repo（CLAUDE.md 規範）。

- [ ] **Step 3: Regenerate frontend types**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend && npm run gen:api
git diff src/api/_generated/schema.d.ts | head -50
```

確認 schema.d.ts 含 `/offboarding/{employee_id}/preview` 等 path key。

- [ ] **Step 4: Commit frontend schema**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend && git add src/api/_generated/schema.d.ts && \
git commit -m "chore(api): regen schema.d.ts for offboarding Phase 1 endpoints

加入 /offboarding/{id}/preview, /process, /{id}, /{id}/nhi-unenroll 4 path。
Phase 1 後端落地（ivy-backend 多 commit ending 在 Task 13）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 5: Verify CI openapi-drift will pass**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend && npm run gen:api:check
```

Expected: 0 diff（剛 regen + commit 完）。

---

## 完成檢核

| 項 | spec ref | task |
|---|---|---|
| Migration offb0001 + SalaryRecord.unused_leave_payout | §4.5 | 1 |
| EmployeeOffboardingRecord model | §4.1 | 1 |
| 特休餘額 helper | §4.5 補 | 2 |
| Orchestrator 殼 + types | §5.1, §5.2 | 3 |
| Step mark_appraisal | §5.3 #1 | 4 |
| Step snapshot_leave + prefill | §5.3 #2,#3 | 5 |
| Step revoke_user | §5.3 #4 | 6 |
| Orchestrator 串接 4 step + rollback | §5.4 | 7 |
| Pydantic schemas + router 殼 | §6.1 | 8 |
| POST /preview | §6.2 | 9 |
| POST /process | §6.3 | 10 |
| GET /{id} | §6.1 row 3 | 11 |
| PATCH /nhi-unenroll | §6.1 row 5 | 11 |
| Aggregator filter 改 | §1 缺口 1 / §14 風險 3 | 12 |
| 舊 endpoint passthrough | §6.5 | 13 |
| Sentry PII denylist | §10.3 | 14 |
| OpenAPI codegen | §12 | 15 |

**Phase 1 不含（Phase 2/3 處理）：** 離職證明 PDF、magic-link、ZIP download、cert.pdf endpoint、前端 UI、Playwright e2e。
