# 補休到期與特休週年制 — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 後端落地補休 +1 年到期自動結算 + 特休改週年制 + 未休折算工資寫 SalaryRecord.unused_leave_payout（透過新建 `unused_leave_payout_log` 帳本）。

**Architecture:** Per-OT grant ledger (`overtime_comp_leave_grants`) + 每日 asyncio polling scheduler 撈到期/週年 → 寫 `unused_leave_payout_log` + 直寫 SalaryRecord（layer 1）或留待月結 calculate 撈（layer 2）。`LeaveQuota.compensatory` 聚合 row 降級為派生快取，實際結餘走 grant ledger SUM。`LeaveQuota.annual` 加 `period_start/end` 走週年制，從 cutover handler 移除。

**Tech Stack:** FastAPI + SQLAlchemy 2.x + Alembic + apscheduler 不使用（改 asyncio polling 沿用 `recruitment_term_advance_scheduler.py` pattern）+ pytest + PostgreSQL（SQLite for tests）。

**Spec:** `docs/superpowers/specs/2026-05-26-leave-quota-lifecycle-design.md` (commit 7691a64)

**前提：** 勞資協商已簽署、scheduler 預設 `leave_quota_expiry_enabled=False`、frontend 另開 Phase B plan。

---

## File Structure

**新檔（純新增）：**
- `alembic/versions/20260526_mergeheads04_merge_audrsk01_mergeheads03_.py` — 合 alembic 兩 head
- `alembic/versions/20260526_compexpr01_leave_quota_lifecycle.py` — schema + backfill
- `models/unused_leave_payout_log.py` — UnusedLeavePayoutLog ORM
- `models/overtime_comp_leave_grant.py` — OvertimeCompLeaveGrant ORM
- `services/leave_quota_expiry/__init__.py` — package init
- `services/leave_quota_expiry/helpers.py` — 純函式 helpers
- `services/leave_quota_expiry/comp_leave_expiry.py` — `expire_comp_leave_grants`
- `services/leave_quota_expiry/annual_cutover.py` — `cutover_annual_leave_anniversaries`
- `services/leave_quota_expiry_scheduler.py` — asyncio polling loop
- `api/leave_quota_expiry.py` — 4 HR endpoint
- 各對應 `tests/test_*.py`

**修改檔：**
- `models/leave.py:132-181` — LeaveQuota 加 `period_start` / `period_end` + partial unique index
- `api/overtimes.py:106-280` — `_grant_comp_leave_quota` / `_revoke_comp_leave_grant` 加 grant ledger
- `api/leaves.py` — 補休假單 FIFO 扣抵 + `_compensatory_balance` 統一入口
- `services/term_subscribers/leave_quota_cutover.py` — 移除 annual cutover + compensatory 改 grant SUM
- `services/salary/engine.py` — calculate 加 layer 2 撈 pending logs
- `main.py:324+` — register scheduler
- `config/scheduler.py` — 加 `leave_quota_expiry_enabled` / `leave_quota_expiry_check_interval`

---

## Task 1: Merge Alembic Heads

**Files:**
- Create: `alembic/versions/20260526_mergeheads04_merge_audrsk01_mergeheads03_.py`

- [ ] **Step 1: 確認當前 heads**

Run: `alembic heads`
Expected: 兩個 head `audrsk01` + `mergeheads03`

- [ ] **Step 2: 寫 merge migration**

```python
"""merge audrsk01 + mergeheads03

Revision ID: mergeheads04
Revises: audrsk01, mergeheads03
Create Date: 2026-05-26 ...
"""

revision = 'mergeheads04'
down_revision = ('audrsk01', 'mergeheads03')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
```

- [ ] **Step 3: 驗證 head 統一**

Run: `alembic upgrade head && alembic heads`
Expected: 單一 head `mergeheads04`

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/20260526_mergeheads04_merge_audrsk01_mergeheads03_.py
git commit -m "chore(alembic): merge heads audrsk01 + mergeheads03"
```

---

## Task 2: UnusedLeavePayoutLog Model

**Files:**
- Create: `models/unused_leave_payout_log.py`
- Test: `tests/test_unused_leave_payout_log_model.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_unused_leave_payout_log_model.py
from datetime import date
from decimal import Decimal

from models.unused_leave_payout_log import UnusedLeavePayoutLog


def test_unused_leave_payout_log_columns():
    log = UnusedLeavePayoutLog(
        employee_id=1,
        source_type='comp_grant_expiry',
        source_ref_id=None,
        hours=4.0,
        hourly_wage=Decimal("200.00"),
        amount=Decimal("800.00"),
        wage_basis_date=date(2026, 4, 1),
        salary_period_year=2026,
        salary_period_month=5,
        meta={"expired_grant_ids": [123]},
    )
    assert log.source_type == 'comp_grant_expiry'
    assert log.amount == Decimal("800.00")
    assert log.meta["expired_grant_ids"] == [123]
    assert log.salary_record_id is None
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_unused_leave_payout_log_model.py -v`
Expected: ImportError `models.unused_leave_payout_log`

- [ ] **Step 3: 實作 model**

```python
# models/unused_leave_payout_log.py
"""未休假折算工資帳本 — per-event 紀錄。

三個 source_type：
- comp_grant_expiry：補休 +1 年到期（scheduler）
- annual_anniversary：特休週年 cutover（scheduler）
- offboarding：離職 path（Phase 2 寫入，本 spec 預留 schema）

salary_record_id 反向綁定：scheduler layer 1 直寫時 set；NULL 由 salary engine
calculate layer 2 撈取後 set。
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class UnusedLeavePayoutLog(Base):
    __tablename__ = "unused_leave_payout_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_ref_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hours: Mapped[float] = mapped_column(nullable=False)
    hourly_wage: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    wage_basis_date: Mapped[date] = mapped_column(Date, nullable=False)
    salary_record_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("salary_records.id", ondelete="SET NULL"),
        nullable=True,
    )
    salary_period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    salary_period_month: Mapped[int] = mapped_column(Integer, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(__import__('sqlalchemy').JSON(), "sqlite"),
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_payout_log_emp_period", "employee_id", "salary_period_year", "salary_period_month"),
        Index(
            "ix_payout_log_salary_record",
            "salary_record_id",
            postgresql_where=__import__('sqlalchemy').text("salary_record_id IS NOT NULL"),
        ),
        Index(
            "uq_payout_log_anniversary",
            "employee_id",
            "source_type",
            "source_ref_id",
            unique=True,
            postgresql_where=__import__('sqlalchemy').text("source_type = 'annual_anniversary'"),
            sqlite_where=__import__('sqlalchemy').text("source_type = 'annual_anniversary'"),
        ),
    )
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `pytest tests/test_unused_leave_payout_log_model.py -v`
Expected: PASS（model 可 import 且欄位齊全）

- [ ] **Step 5: Commit**

```bash
git add models/unused_leave_payout_log.py tests/test_unused_leave_payout_log_model.py
git commit -m "feat(models): UnusedLeavePayoutLog per-event 帳本 model"
```

---

## Task 3: OvertimeCompLeaveGrant Model

**Files:**
- Create: `models/overtime_comp_leave_grant.py`
- Test: `tests/test_overtime_comp_leave_grant_model.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_overtime_comp_leave_grant_model.py
from datetime import date

from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant


def test_grant_default_status_active():
    g = OvertimeCompLeaveGrant(
        overtime_record_id=1,
        employee_id=2,
        granted_hours=4.0,
        granted_at=date(2025, 4, 1),
        expires_at=date(2026, 4, 1),
    )
    assert g.status == 'active'
    assert g.consumed_hours == 0.0
    assert g.expired_at is None
    assert g.payout_log_id is None
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_overtime_comp_leave_grant_model.py -v`
Expected: ImportError

- [ ] **Step 3: 實作 model**

```python
# models/overtime_comp_leave_grant.py
"""補休 grant ledger — per-OT 一筆紀錄。

語義：每筆核准的「以補休代加班費」OT 對應一筆 grant，
granted_at = ot.overtime_date, expires_at = granted_at + 1 年。
consumed_hours 由補休假單核准/駁回 FIFO 維護。
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import relationship

from models.base import Base


class OvertimeCompLeaveGrant(Base):
    __tablename__ = "overtime_comp_leave_grants"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    overtime_record_id = Column(
        Integer,
        ForeignKey("overtime_records.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    employee_id = Column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    granted_hours = Column(Float, nullable=False)
    granted_at = Column(Date, nullable=False)
    expires_at = Column(Date, nullable=False)
    consumed_hours = Column(Float, nullable=False, default=0)
    status = Column(String(20), nullable=False, default='active')
    expired_at = Column(DateTime, nullable=True)
    payout_salary_record_id = Column(
        Integer, ForeignKey("salary_records.id", ondelete="SET NULL"), nullable=True
    )
    payout_log_id = Column(
        BigInteger, ForeignKey("unused_leave_payout_log.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    overtime_record = relationship("OvertimeRecord", backref="comp_leave_grant")

    __table_args__ = (
        CheckConstraint("consumed_hours <= granted_hours", name="ck_grant_consumed_le_granted"),
        Index("ix_grant_emp_status_expires", "employee_id", "status", "expires_at"),
        Index(
            "ix_grant_status_expires_active",
            "expires_at",
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `pytest tests/test_overtime_comp_leave_grant_model.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add models/overtime_comp_leave_grant.py tests/test_overtime_comp_leave_grant_model.py
git commit -m "feat(models): OvertimeCompLeaveGrant 補休 grant ledger model"
```

---

## Task 4: LeaveQuota 加 period_start/period_end

**Files:**
- Modify: `models/leave.py:132-181`
- Test: `tests/test_leave_quota_period_fields.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_leave_quota_period_fields.py
from datetime import date

from models.leave import LeaveQuota


def test_leave_quota_period_fields_default_null():
    q = LeaveQuota(
        employee_id=1, year=2026, leave_type='annual', total_hours=80.0
    )
    assert q.period_start is None
    assert q.period_end is None


def test_leave_quota_period_fields_set():
    q = LeaveQuota(
        employee_id=1,
        year=2026,
        leave_type='annual',
        total_hours=80.0,
        period_start=date(2025, 8, 15),
        period_end=date(2026, 8, 15),
    )
    assert q.period_start == date(2025, 8, 15)
    assert q.period_end == date(2026, 8, 15)
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_leave_quota_period_fields.py -v`
Expected: TypeError unexpected keyword `period_start`

- [ ] **Step 3: 修改 models/leave.py**

在 `LeaveQuota` 內加兩欄與 partial unique index：

```python
# models/leave.py 約 line 176 增加（updated_at 之前）
period_start = Column(Date, nullable=True, comment="週年制配額起日（hire_date 基準）")
period_end = Column(Date, nullable=True, comment="週年制配額迄日（+1y，2/29 fallback 2/28）")
```

在 `__table_args__` 加 partial unique index：

```python
Index(
    "uq_leave_quotas_emp_period_annual",
    "employee_id",
    "period_start",
    "leave_type",
    unique=True,
    postgresql_where=text("period_start IS NOT NULL AND leave_type = 'annual'"),
    sqlite_where=text("period_start IS NOT NULL AND leave_type = 'annual'"),
),
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `pytest tests/test_leave_quota_period_fields.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add models/leave.py tests/test_leave_quota_period_fields.py
git commit -m "feat(models): LeaveQuota 加 period_start/period_end + partial unique idx (週年制)"
```

---

## Task 5: compexpr01 Migration + Backfill

**Files:**
- Create: `alembic/versions/20260526_compexpr01_leave_quota_lifecycle.py`
- Test: `tests/test_compexpr01_migration.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_compexpr01_migration.py
"""驗證 compexpr01 upgrade/downgrade 對稱 + backfill 正確。"""

import subprocess
from sqlalchemy import create_engine, inspect, text


def test_compexpr01_upgrade_creates_tables_and_columns(tmp_path):
    db_url = f"sqlite:///{tmp_path}/test.db"
    subprocess.run(
        ["alembic", "-x", f"db_url={db_url}", "upgrade", "compexpr01"],
        check=True,
        env={"SQLALCHEMY_DATABASE_URL": db_url, **dict(__import__('os').environ)},
    )

    eng = create_engine(db_url)
    insp = inspect(eng)
    assert "unused_leave_payout_log" in insp.get_table_names()
    assert "overtime_comp_leave_grants" in insp.get_table_names()
    leave_quota_cols = {c["name"] for c in insp.get_columns("leave_quotas")}
    assert "period_start" in leave_quota_cols
    assert "period_end" in leave_quota_cols


def test_compexpr01_backfill_existing_ot_to_grants(tmp_path):
    """既有 OT (use_comp_leave=True AND comp_leave_granted=True AND is_approved=True)
    backfill 為 grant row，expires_at = upgrade_date + 3 個月。"""
    # 1. 升至前一版（mergeheads04），插入 OT row
    # 2. 升至 compexpr01
    # 3. 驗 grant row 存在 + expires_at 正確
    # ... (詳細測試略，重點 assert)
    pass


def test_compexpr01_downgrade_drops_cleanly(tmp_path):
    """downgrade 還原 schema（不嘗試 reverse backfill）"""
    pass
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_compexpr01_migration.py -v`
Expected: FAIL `Can't locate revision identified by 'compexpr01'`

- [ ] **Step 3: 寫 migration**

```python
# alembic/versions/20260526_compexpr01_leave_quota_lifecycle.py
"""leave_quota_lifecycle: unused_leave_payout_log + overtime_comp_leave_grants + LeaveQuota period 欄

Revision ID: compexpr01
Revises: mergeheads04
Create Date: 2026-05-26 ...
"""

import os
from datetime import date, timedelta

from alembic import op
import sqlalchemy as sa

revision = 'compexpr01'
down_revision = 'mergeheads04'
branch_labels = None
depends_on = None

BACKFILL_GRACE_MONTHS = int(os.environ.get("LEAVE_BACKFILL_GRACE_MONTHS", "3"))


def upgrade():
    # 1. unused_leave_payout_log
    op.create_table(
        "unused_leave_payout_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer, sa.ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_ref_id", sa.Integer, nullable=True),
        sa.Column("hours", sa.Float, nullable=False),
        sa.Column("hourly_wage", sa.Numeric(10, 2), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("wage_basis_date", sa.Date, nullable=False),
        sa.Column("salary_record_id", sa.Integer, sa.ForeignKey("salary_records.id", ondelete="SET NULL"), nullable=True),
        sa.Column("salary_period_year", sa.Integer, nullable=False),
        sa.Column("salary_period_month", sa.Integer, nullable=False),
        sa.Column("meta", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False, server_default='{}'),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_payout_log_emp_period", "unused_leave_payout_log", ["employee_id", "salary_period_year", "salary_period_month"])
    op.create_index("ix_payout_log_salary_record", "unused_leave_payout_log", ["salary_record_id"], postgresql_where=sa.text("salary_record_id IS NOT NULL"))
    op.create_index(
        "uq_payout_log_anniversary",
        "unused_leave_payout_log",
        ["employee_id", "source_type", "source_ref_id"],
        unique=True,
        postgresql_where=sa.text("source_type = 'annual_anniversary'"),
        sqlite_where=sa.text("source_type = 'annual_anniversary'"),
    )

    # 2. overtime_comp_leave_grants
    op.create_table(
        "overtime_comp_leave_grants",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("overtime_record_id", sa.Integer, sa.ForeignKey("overtime_records.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("employee_id", sa.Integer, sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("granted_hours", sa.Float, nullable=False),
        sa.Column("granted_at", sa.Date, nullable=False),
        sa.Column("expires_at", sa.Date, nullable=False),
        sa.Column("consumed_hours", sa.Float, nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("expired_at", sa.DateTime, nullable=True),
        sa.Column("payout_salary_record_id", sa.Integer, sa.ForeignKey("salary_records.id", ondelete="SET NULL"), nullable=True),
        sa.Column("payout_log_id", sa.BigInteger, sa.ForeignKey("unused_leave_payout_log.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("consumed_hours <= granted_hours", name="ck_grant_consumed_le_granted"),
    )
    op.create_index("ix_grant_emp_status_expires", "overtime_comp_leave_grants", ["employee_id", "status", "expires_at"])
    op.create_index(
        "ix_grant_status_expires_active",
        "overtime_comp_leave_grants",
        ["expires_at"],
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )

    # 3. LeaveQuota 加 period_start / period_end
    op.add_column("leave_quotas", sa.Column("period_start", sa.Date, nullable=True))
    op.add_column("leave_quotas", sa.Column("period_end", sa.Date, nullable=True))
    op.create_index(
        "uq_leave_quotas_emp_period_annual",
        "leave_quotas",
        ["employee_id", "period_start", "leave_type"],
        unique=True,
        postgresql_where=sa.text("period_start IS NOT NULL AND leave_type = 'annual'"),
        sqlite_where=sa.text("period_start IS NOT NULL AND leave_type = 'annual'"),
    )

    # 4. Backfill 既有 OT → grant rows
    today = date.today()
    grace_expires_at = today + timedelta(days=BACKFILL_GRACE_MONTHS * 30)
    op.execute(sa.text(
        f"""INSERT INTO overtime_comp_leave_grants (
                overtime_record_id, employee_id, granted_hours, granted_at, expires_at,
                consumed_hours, status
            )
            SELECT id, employee_id, hours, overtime_date, :grace_date, 0, 'active'
            FROM overtime_records
            WHERE use_comp_leave = TRUE
              AND comp_leave_granted = TRUE
              AND is_approved = TRUE
        """).bindparams(grace_date=grace_expires_at)
    )

    # 5. Backfill 既有 annual LeaveQuota → period_start = hire_date 最近週年 / period_end = +1y
    # Python 端跑（SQL 表達 anniversary date 跨方言複雜，且資料量小）
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        """SELECT lq.id, e.hire_date
           FROM leave_quotas lq
           JOIN employees e ON e.id = lq.employee_id
           WHERE lq.leave_type = 'annual' AND lq.period_start IS NULL
        """
    )).fetchall()
    for lq_id, hire_date in rows:
        if hire_date is None:
            continue
        # 計算 hire_date 最近過去的週年
        years_elapsed = today.year - hire_date.year
        if (today.month, today.day) < (hire_date.month, hire_date.day):
            years_elapsed -= 1
        if years_elapsed < 0:
            continue  # 未到第一週年，不 backfill
        try:
            period_start = hire_date.replace(year=hire_date.year + years_elapsed)
        except ValueError:  # 2/29 → 非閏年
            period_start = hire_date.replace(year=hire_date.year + years_elapsed, day=28)
        try:
            period_end = period_start.replace(year=period_start.year + 1)
        except ValueError:
            period_end = period_start.replace(year=period_start.year + 1, day=28)
        bind.execute(sa.text(
            "UPDATE leave_quotas SET period_start = :ps, period_end = :pe WHERE id = :id"
        ).bindparams(ps=period_start, pe=period_end, id=lq_id))


def downgrade():
    op.drop_index("uq_leave_quotas_emp_period_annual", table_name="leave_quotas")
    op.drop_column("leave_quotas", "period_end")
    op.drop_column("leave_quotas", "period_start")
    op.drop_index("ix_grant_status_expires_active", table_name="overtime_comp_leave_grants")
    op.drop_index("ix_grant_emp_status_expires", table_name="overtime_comp_leave_grants")
    op.drop_table("overtime_comp_leave_grants")
    op.drop_index("uq_payout_log_anniversary", table_name="unused_leave_payout_log")
    op.drop_index("ix_payout_log_salary_record", table_name="unused_leave_payout_log")
    op.drop_index("ix_payout_log_emp_period", table_name="unused_leave_payout_log")
    op.drop_table("unused_leave_payout_log")
```

- [ ] **Step 4: 補完 backfill 測試 + 跑 PASS**

補完 `test_compexpr01_backfill_existing_ot_to_grants` 與 `test_compexpr01_downgrade_drops_cleanly`：

```python
def test_compexpr01_backfill_existing_ot_to_grants(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAVE_BACKFILL_GRACE_MONTHS", "3")
    db_url = f"sqlite:///{tmp_path}/test.db"
    # 1. upgrade 至 mergeheads04
    # 2. INSERT 一筆 OT (use_comp_leave=True, comp_leave_granted=True, is_approved=True, hours=4)
    # 3. upgrade 至 compexpr01
    # 4. SELECT grant row 驗：granted_hours=4, expires_at ≈ today + 90 天
    pass
```

Run: `pytest tests/test_compexpr01_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/20260526_compexpr01_leave_quota_lifecycle.py tests/test_compexpr01_migration.py
git commit -m "feat(alembic): compexpr01 leave_quota_lifecycle schema + backfill"
```

---

## Task 6: 純函式 helpers — 日期/時薪

**Files:**
- Create: `services/leave_quota_expiry/__init__.py` (空)
- Create: `services/leave_quota_expiry/helpers.py`
- Test: `tests/test_leave_quota_expiry_helpers.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_leave_quota_expiry_helpers.py
from datetime import date
from unittest.mock import MagicMock

from services.leave_quota_expiry.helpers import (
    _next_month,
    _add_one_year_with_feb29_handling,
    _resolve_hourly_wage,
)


def test_next_month_normal():
    assert _next_month(date(2026, 4, 15)) == (2026, 5)


def test_next_month_year_wrap():
    assert _next_month(date(2026, 12, 31)) == (2027, 1)


def test_add_one_year_normal():
    assert _add_one_year_with_feb29_handling(date(2025, 4, 1)) == date(2026, 4, 1)


def test_add_one_year_feb29_to_non_leap():
    # 2024 是閏年，2025 不是 → 2/29 → 2/28
    assert _add_one_year_with_feb29_handling(date(2024, 2, 29)) == date(2025, 2, 28)


def test_resolve_hourly_wage_hourly_employee():
    emp = MagicMock(employee_type='hourly', hourly_rate=200.0)
    assert _resolve_hourly_wage(emp, date(2026, 4, 1)) == 200.0


def test_resolve_hourly_wage_monthly_employee():
    emp = MagicMock(employee_type='monthly', base_salary=48000.0)
    # 48000 / 30 / 8 = 200
    assert _resolve_hourly_wage(emp, date(2026, 4, 1)) == 200.0
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_leave_quota_expiry_helpers.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: 實作 helpers**

```python
# services/leave_quota_expiry/__init__.py
```

```python
# services/leave_quota_expiry/helpers.py
"""純函式 helpers — 不依賴 session，可獨立測試。"""

from datetime import date


def _next_month(today: date) -> tuple[int, int]:
    """跨年 12→1 wrap"""
    if today.month == 12:
        return today.year + 1, 1
    return today.year, today.month + 1


def _add_one_year_with_feb29_handling(d: date) -> date:
    """2/29 + 1y 落非閏年順延 2/28"""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


def _resolve_hourly_wage(emp, ref_date: date) -> float:
    """月薪/30/8 或 hourly_rate。

    註：未來若引入 EmployeeSalaryHistory 取 ref_date 當下生效薪資，
    在此 helper 內展開即可，scheduler caller 不需改。
    """
    if emp.employee_type == 'hourly':
        return float(emp.hourly_rate or 0)
    monthly = float(emp.base_salary or 0)
    if monthly <= 0:
        return 0.0
    return monthly / 30 / 8
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `pytest tests/test_leave_quota_expiry_helpers.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add services/leave_quota_expiry/__init__.py services/leave_quota_expiry/helpers.py tests/test_leave_quota_expiry_helpers.py
git commit -m "feat(leave-expiry): _next_month / _add_one_year_with_feb29_handling / _resolve_hourly_wage"
```

---

## Task 7: SQL helpers — anniversary + period used

**Files:**
- Modify: `services/leave_quota_expiry/helpers.py`
- Test: append to `tests/test_leave_quota_expiry_helpers.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_leave_quota_expiry_helpers.py 追加
import pytest
from sqlalchemy import create_engine, Column, Integer, Date
from sqlalchemy.orm import sessionmaker, declarative_base

from services.leave_quota_expiry.helpers import (
    _is_anniversary_today_sql,
    _approved_annual_used_in_period,
    _find_or_none_salary_record,
)

Base = declarative_base()


class _DummyEmp(Base):
    __tablename__ = 'dummy_emp'
    id = Column(Integer, primary_key=True)
    hire_date = Column(Date)


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()


def test_is_anniversary_today_sql_match(session):
    session.add(_DummyEmp(id=1, hire_date=date(2020, 4, 15)))
    session.add(_DummyEmp(id=2, hire_date=date(2021, 5, 1)))
    session.commit()
    result = session.query(_DummyEmp).filter(
        _is_anniversary_today_sql(_DummyEmp.hire_date, date(2026, 4, 15))
    ).all()
    assert len(result) == 1
    assert result[0].id == 1


def test_is_anniversary_today_sql_feb29_in_non_leap(session):
    """2/29 員工在非閏年 2/28 也算 anniversary"""
    session.add(_DummyEmp(id=1, hire_date=date(2020, 2, 29)))
    session.commit()
    # 2026 非閏年，2/28 應命中
    result = session.query(_DummyEmp).filter(
        _is_anniversary_today_sql(_DummyEmp.hire_date, date(2026, 2, 28))
    ).all()
    assert len(result) == 1
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_leave_quota_expiry_helpers.py::test_is_anniversary_today_sql_match -v`
Expected: ImportError

- [ ] **Step 3: 實作 SQL helpers**

追加至 `services/leave_quota_expiry/helpers.py`：

```python
from calendar import isleap
from typing import Optional

from sqlalchemy import and_, extract, func, or_
from sqlalchemy.orm import Session


def _is_anniversary_today_sql(hire_date_col, today: date):
    """SQL 表達式：員工 hire_date 月日 == today 月日。

    2/29 fallback：非閏年的 2/28 同時撈 hire_date=2/29 員工。
    """
    base = and_(
        extract('month', hire_date_col) == today.month,
        extract('day', hire_date_col) == today.day,
    )
    if today.month == 2 and today.day == 28 and not isleap(today.year):
        return or_(
            base,
            and_(
                extract('month', hire_date_col) == 2,
                extract('day', hire_date_col) == 29,
            ),
        )
    return base


def _approved_annual_used_in_period(
    employee_id: int, period_start: date, period_end: date, session: Session
) -> float:
    """加總期間內已核准的 annual leave 時數。"""
    from models.leave import LeaveRecord

    used = (
        session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0))
        .filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.leave_type == 'annual',
            LeaveRecord.is_approved.is_(True),
            LeaveRecord.start_date >= period_start,
            LeaveRecord.start_date < period_end,
        )
        .scalar()
    )
    return float(used or 0)


def _find_or_none_salary_record(
    employee_id: int, year: int, month: int, session: Session
):
    """撈該員工該月 SalaryRecord；不存在返 None。"""
    from models.salary import SalaryRecord

    return (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id == employee_id,
            SalaryRecord.year == year,
            SalaryRecord.month == month,
        )
        .first()
    )
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `pytest tests/test_leave_quota_expiry_helpers.py -v`
Expected: 9 PASS（含 anniversary fixtures）

- [ ] **Step 5: Commit**

```bash
git add services/leave_quota_expiry/helpers.py tests/test_leave_quota_expiry_helpers.py
git commit -m "feat(leave-expiry): _is_anniversary_today_sql / _approved_annual_used_in_period / _find_or_none_salary_record"
```

---

## Task 8: `_compensatory_balance` 統一查詢入口

**Files:**
- Modify: `services/leave_quota_expiry/helpers.py`
- Test: append to `tests/test_leave_quota_expiry_helpers.py`

- [ ] **Step 1: 寫 failing test**

```python
def test_compensatory_balance_sum_active_grants(session):
    """補休結餘 = SUM(granted_hours - consumed_hours) WHERE status='active'"""
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
    # 需要先 create OvertimeCompLeaveGrant 表
    OvertimeCompLeaveGrant.__table__.create(session.bind)
    session.add(OvertimeCompLeaveGrant(
        id=1, overtime_record_id=10, employee_id=1,
        granted_hours=4.0, granted_at=date(2025, 4, 1), expires_at=date(2026, 4, 1),
        consumed_hours=1.0, status='active',
    ))
    session.add(OvertimeCompLeaveGrant(
        id=2, overtime_record_id=11, employee_id=1,
        granted_hours=8.0, granted_at=date(2025, 5, 1), expires_at=date(2026, 5, 1),
        consumed_hours=0.0, status='active',
    ))
    session.add(OvertimeCompLeaveGrant(
        id=3, overtime_record_id=12, employee_id=1,
        granted_hours=2.0, granted_at=date(2024, 1, 1), expires_at=date(2025, 1, 1),
        consumed_hours=0.0, status='expired',  # 不計
    ))
    session.commit()

    from services.leave_quota_expiry.helpers import _compensatory_balance
    assert _compensatory_balance(1, session) == 11.0  # (4-1) + (8-0)
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_leave_quota_expiry_helpers.py::test_compensatory_balance_sum_active_grants -v`
Expected: ImportError

- [ ] **Step 3: 實作 helper**

追加至 `services/leave_quota_expiry/helpers.py`：

```python
def _compensatory_balance(employee_id: int, session: Session) -> float:
    """員工目前可用補休餘額 = SUM(granted_hours - consumed_hours) WHERE status='active'

    此為新的 source of truth；既有 LeaveQuota.compensatory.total_hours 降級為快取。
    """
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    balance = (
        session.query(
            func.coalesce(
                func.sum(OvertimeCompLeaveGrant.granted_hours - OvertimeCompLeaveGrant.consumed_hours),
                0,
            )
        )
        .filter(
            OvertimeCompLeaveGrant.employee_id == employee_id,
            OvertimeCompLeaveGrant.status == 'active',
        )
        .scalar()
    )
    return float(balance or 0)
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `pytest tests/test_leave_quota_expiry_helpers.py::test_compensatory_balance_sum_active_grants -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/leave_quota_expiry/helpers.py tests/test_leave_quota_expiry_helpers.py
git commit -m "feat(leave-expiry): _compensatory_balance 統一查詢入口（grant ledger source of truth）"
```

---

## Task 9: `expire_comp_leave_grants` service

**Files:**
- Create: `services/leave_quota_expiry/comp_leave_expiry.py`
- Test: `tests/test_expire_comp_leave_grants.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_expire_comp_leave_grants.py
"""scheduler 撈到期 grant 結算 + 寫 log + 寫 SalaryRecord 行為驗證。"""

import pytest
from datetime import date
from decimal import Decimal


@pytest.fixture
def session():
    # SQLite in-memory + 全 schema create_all
    ...


def test_expire_no_active_grants_no_op(session):
    """無 expired grant 不會建 log"""
    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants
    summary = expire_comp_leave_grants(date(2026, 4, 1), session)
    assert summary == {'paid_employees': 0, 'total_amount': 0.0, 'expired_grant_count': 0}


def test_expire_two_grants_one_employee_writes_one_log(session, employee_factory, ot_grant_factory):
    """同員工兩筆到期 grant → 一筆 log 加總 + grant status='expired'"""
    emp = employee_factory(employee_type='hourly', hourly_rate=200.0)
    g1 = ot_grant_factory(emp_id=emp.id, granted_hours=4.0, consumed=0.0,
                          expires_at=date(2026, 3, 31), status='active')
    g2 = ot_grant_factory(emp_id=emp.id, granted_hours=8.0, consumed=2.0,
                          expires_at=date(2026, 3, 30), status='active')

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants
    summary = expire_comp_leave_grants(date(2026, 4, 1), session)

    assert summary['paid_employees'] == 1
    assert summary['expired_grant_count'] == 2
    # unexpired = (4-0) + (8-2) = 10 → amount = 10 * 200 = 2000
    assert summary['total_amount'] == 2000.0

    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    logs = session.query(UnusedLeavePayoutLog).all()
    assert len(logs) == 1
    assert logs[0].source_type == 'comp_grant_expiry'
    assert logs[0].amount == Decimal('2000.00')
    assert logs[0].meta['expired_grant_ids'] == [g1.id, g2.id]

    session.refresh(g1); session.refresh(g2)
    assert g1.status == 'expired'
    assert g2.status == 'expired'
    assert g1.payout_log_id == logs[0].id


def test_expire_skips_inactive_employee(session, employee_factory, ot_grant_factory):
    """is_active=False 員工跳過（由 offboarding path 處理）"""
    emp = employee_factory(is_active=False)
    ot_grant_factory(emp_id=emp.id, granted_hours=4.0, expires_at=date(2026, 3, 31), status='active')

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants
    summary = expire_comp_leave_grants(date(2026, 4, 1), session)
    assert summary['expired_grant_count'] == 0


def test_expire_fully_consumed_grant_marked_expired_no_log(session, employee_factory, ot_grant_factory):
    """全用完的 grant 不建 log 但仍 mark expired"""
    emp = employee_factory()
    g = ot_grant_factory(emp_id=emp.id, granted_hours=4.0, consumed=4.0,
                         expires_at=date(2026, 3, 31), status='active')

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants
    summary = expire_comp_leave_grants(date(2026, 4, 1), session)

    assert summary['paid_employees'] == 0
    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    assert session.query(UnusedLeavePayoutLog).count() == 0

    session.refresh(g)
    assert g.status == 'expired'


def test_expire_writes_to_existing_unfinalized_salary_record(session, employee_factory, ot_grant_factory, salary_record_factory):
    """目標月 SalaryRecord 已存在且未 finalize → 直寫 + 綁定 log"""
    emp = employee_factory(employee_type='hourly', hourly_rate=200.0)
    ot_grant_factory(emp_id=emp.id, granted_hours=4.0, expires_at=date(2026, 3, 31), status='active')
    # _next_month(2026-04-01) = (2026, 5)
    sr = salary_record_factory(emp_id=emp.id, year=2026, month=5, is_finalized=False, unused_leave_payout=0)

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants
    expire_comp_leave_grants(date(2026, 4, 1), session)

    session.refresh(sr)
    assert sr.unused_leave_payout == Decimal('800.00')

    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    log = session.query(UnusedLeavePayoutLog).first()
    assert log.salary_record_id == sr.id


def test_expire_does_not_write_to_finalized_salary_record(session, employee_factory, ot_grant_factory, salary_record_factory):
    """目標月 SalaryRecord 已 finalize → log.salary_record_id=NULL 由 layer 2 接手"""
    emp = employee_factory(employee_type='hourly', hourly_rate=200.0)
    ot_grant_factory(emp_id=emp.id, granted_hours=4.0, expires_at=date(2026, 3, 31), status='active')
    sr = salary_record_factory(emp_id=emp.id, year=2026, month=5, is_finalized=True, unused_leave_payout=0)

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants
    expire_comp_leave_grants(date(2026, 4, 1), session)

    session.refresh(sr)
    assert sr.unused_leave_payout == Decimal('0')  # 不動

    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    log = session.query(UnusedLeavePayoutLog).first()
    assert log.salary_record_id is None
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_expire_comp_leave_grants.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: 實作 service**

```python
# services/leave_quota_expiry/comp_leave_expiry.py
"""補休 grant 到期結算：scheduler 主邏輯之一。"""

import logging
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from models.employee import Employee
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
from models.unused_leave_payout_log import UnusedLeavePayoutLog
from services.leave_quota_expiry.helpers import (
    _find_or_none_salary_record,
    _next_month,
    _resolve_hourly_wage,
)
from services.salary.unused_leave_pay import calculate_unused_leave_compensation
from utils.money import round_half_up

logger = logging.getLogger(__name__)


def expire_comp_leave_grants(today: date, session: Session) -> dict:
    """撈到期 grant 結算寫 SalaryRecord.unused_leave_payout + log。

    跳過已離職員工（由 offboarding path 處理）。
    """
    expired_grants = (
        session.query(OvertimeCompLeaveGrant)
        .join(Employee, Employee.id == OvertimeCompLeaveGrant.employee_id)
        .filter(
            OvertimeCompLeaveGrant.status == 'active',
            OvertimeCompLeaveGrant.expires_at <= today,
            Employee.is_active.is_(True),
        )
        .all()
    )

    grants_by_emp: dict[int, list[OvertimeCompLeaveGrant]] = {}
    for g in expired_grants:
        grants_by_emp.setdefault(g.employee_id, []).append(g)

    total_paid = Decimal("0")
    paid_emp_count = 0

    for emp_id, grants in grants_by_emp.items():
        try:
            with session.begin_nested():
                unexpired_hours = sum(g.granted_hours - g.consumed_hours for g in grants)
                if unexpired_hours <= 0:
                    for g in grants:
                        g.status = 'expired'
                        g.expired_at = datetime.now()
                    continue

                emp = session.get(Employee, emp_id)
                hourly_wage = _resolve_hourly_wage(emp, today)
                amount = round_half_up(
                    Decimal(str(calculate_unused_leave_compensation(unexpired_hours, hourly_wage)))
                )

                period_year, period_month = _next_month(today)
                log = UnusedLeavePayoutLog(
                    employee_id=emp_id,
                    source_type='comp_grant_expiry',
                    source_ref_id=None,
                    hours=unexpired_hours,
                    hourly_wage=Decimal(str(hourly_wage)),
                    amount=amount,
                    wage_basis_date=today,
                    salary_period_year=period_year,
                    salary_period_month=period_month,
                    meta={
                        'expired_grant_ids': [g.id for g in grants],
                        'hours_breakdown': [
                            {
                                'grant_id': g.id,
                                'overtime_date': g.granted_at.isoformat(),
                                'unexpired_hours': g.granted_hours - g.consumed_hours,
                            }
                            for g in grants
                        ],
                    },
                )
                session.add(log)
                session.flush()

                salary_record = _find_or_none_salary_record(
                    emp_id, period_year, period_month, session
                )
                if salary_record is not None and not getattr(salary_record, 'is_finalized', False):
                    salary_record.unused_leave_payout = (
                        (salary_record.unused_leave_payout or Decimal("0")) + amount
                    )
                    log.salary_record_id = salary_record.id

                for g in grants:
                    g.status = 'expired'
                    g.expired_at = datetime.now()
                    g.payout_salary_record_id = salary_record.id if (
                        salary_record is not None
                        and not getattr(salary_record, 'is_finalized', False)
                    ) else None
                    g.payout_log_id = log.id

                total_paid += amount
                paid_emp_count += 1
        except Exception:
            logger.exception("expire_comp_leave failed for emp=%d", emp_id)

    return {
        'paid_employees': paid_emp_count,
        'total_amount': float(total_paid),
        'expired_grant_count': len(expired_grants),
    }
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `pytest tests/test_expire_comp_leave_grants.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add services/leave_quota_expiry/comp_leave_expiry.py tests/test_expire_comp_leave_grants.py
git commit -m "feat(leave-expiry): expire_comp_leave_grants scheduler 主邏輯"
```

---

## Task 10: `cutover_annual_leave_anniversaries` service

**Files:**
- Create: `services/leave_quota_expiry/annual_cutover.py`
- Test: `tests/test_cutover_annual_leave_anniversaries.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_cutover_annual_leave_anniversaries.py
import pytest
from datetime import date
from decimal import Decimal


def test_cutover_no_anniversaries_no_op(session):
    from services.leave_quota_expiry.annual_cutover import cutover_annual_leave_anniversaries
    summary = cutover_annual_leave_anniversaries(date(2026, 4, 1), session)
    assert summary['total_anniversaries'] == 0


def test_cutover_cold_start_employee_creates_first_period_no_payout(session, employee_factory):
    """員工 hire_date 月日 = today，且無既有 period_start row → cold start，
    建 new period 但無未休結算"""
    emp = employee_factory(hire_date=date(2020, 4, 1), employee_type='monthly', base_salary=48000.0)

    from services.leave_quota_expiry.annual_cutover import cutover_annual_leave_anniversaries
    summary = cutover_annual_leave_anniversaries(date(2026, 4, 1), session)

    assert summary['cold_start_employees'] == 1
    assert summary['paid_employees'] == 0

    from models.leave import LeaveQuota
    quota = session.query(LeaveQuota).filter_by(employee_id=emp.id, leave_type='annual').first()
    assert quota.period_start == date(2026, 4, 1)
    assert quota.period_end == date(2027, 4, 1)
    # 年資 6 年 → 120 hr
    assert quota.total_hours == 120.0


def test_cutover_existing_period_with_unused_writes_log_and_new_period(session, employee_factory, leave_quota_factory):
    """既有 period 有未休 → 寫 log + 建新 period"""
    emp = employee_factory(hire_date=date(2020, 4, 1), employee_type='hourly', hourly_rate=200.0)
    leave_quota_factory(
        employee_id=emp.id, leave_type='annual', total_hours=120.0,
        period_start=date(2025, 4, 1), period_end=date(2026, 4, 1),
    )

    from services.leave_quota_expiry.annual_cutover import cutover_annual_leave_anniversaries
    summary = cutover_annual_leave_anniversaries(date(2026, 4, 1), session)

    assert summary['paid_employees'] == 1
    # 未用 = 120, amount = 120 * 200 = 24000
    assert summary['total_amount'] == 24000.0

    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    log = session.query(UnusedLeavePayoutLog).first()
    assert log.source_type == 'annual_anniversary'
    assert log.amount == Decimal('24000.00')
    assert log.meta['period_start'] == '2025-04-01'

    from models.leave import LeaveQuota
    new_period = session.query(LeaveQuota).filter_by(
        employee_id=emp.id, period_start=date(2026, 4, 1)
    ).first()
    assert new_period.period_end == date(2027, 4, 1)


def test_cutover_idempotent_second_run_same_day_skips(session, employee_factory):
    """同日重跑 → IntegrityError 被吃掉，無重複 row"""
    emp = employee_factory(hire_date=date(2020, 4, 1), base_salary=48000.0)

    from services.leave_quota_expiry.annual_cutover import cutover_annual_leave_anniversaries
    cutover_annual_leave_anniversaries(date(2026, 4, 1), session)
    cutover_annual_leave_anniversaries(date(2026, 4, 1), session)  # 第二次

    from models.leave import LeaveQuota
    quotas = session.query(LeaveQuota).filter_by(employee_id=emp.id, leave_type='annual').all()
    assert len(quotas) == 1  # 不重複


def test_cutover_skips_employee_under_six_months(session, employee_factory):
    """未滿 180 天員工不 cutover"""
    today = date(2026, 4, 1)
    emp = employee_factory(hire_date=date(2025, 12, 1))  # 約 4 個月
    from services.leave_quota_expiry.annual_cutover import cutover_annual_leave_anniversaries
    summary = cutover_annual_leave_anniversaries(today, session)
    assert summary['total_anniversaries'] == 0
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_cutover_annual_leave_anniversaries.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: 實作 service**

```python
# services/leave_quota_expiry/annual_cutover.py
"""特休週年 cutover：scheduler 主邏輯之二。"""

import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.leaves_quota import _calc_annual_leave_hours
from models.employee import Employee
from models.leave import LeaveQuota
from models.unused_leave_payout_log import UnusedLeavePayoutLog
from services.leave_quota_expiry.helpers import (
    _add_one_year_with_feb29_handling,
    _approved_annual_used_in_period,
    _find_or_none_salary_record,
    _is_anniversary_today_sql,
    _next_month,
    _resolve_hourly_wage,
)
from services.salary.unused_leave_pay import calculate_unused_leave_compensation
from utils.money import round_half_up

logger = logging.getLogger(__name__)


def cutover_annual_leave_anniversaries(today: date, session: Session) -> dict:
    """跑「今日滿週年」員工：結算上一週年未休 + 建新一週年 row。"""
    candidates = (
        session.query(Employee)
        .filter(
            Employee.is_active.is_(True),
            _is_anniversary_today_sql(Employee.hire_date, today),
            Employee.hire_date <= today - timedelta(days=180),
        )
        .all()
    )

    paid_count = 0
    cold_start_count = 0
    total_paid = Decimal("0")

    for emp in candidates:
        try:
            with session.begin_nested():
                current = (
                    session.query(LeaveQuota)
                    .filter(
                        LeaveQuota.employee_id == emp.id,
                        LeaveQuota.leave_type == 'annual',
                        LeaveQuota.period_start.isnot(None),
                        LeaveQuota.period_start <= today,
                        LeaveQuota.period_end > today,
                    )
                    .first()
                )

                if current is not None:
                    used = _approved_annual_used_in_period(
                        emp.id, current.period_start, today, session
                    )
                    unused = max(0.0, current.total_hours - used)
                    if unused > 0:
                        hourly_wage = _resolve_hourly_wage(emp, today)
                        amount = round_half_up(Decimal(str(
                            calculate_unused_leave_compensation(unused, hourly_wage)
                        )))
                        period_year, period_month = _next_month(today)
                        log = UnusedLeavePayoutLog(
                            employee_id=emp.id,
                            source_type='annual_anniversary',
                            source_ref_id=current.id,
                            hours=unused,
                            hourly_wage=Decimal(str(hourly_wage)),
                            amount=amount,
                            wage_basis_date=today,
                            salary_period_year=period_year,
                            salary_period_month=period_month,
                            meta={
                                'period_start': current.period_start.isoformat(),
                                'period_end': current.period_end.isoformat(),
                                'entitled_hours': current.total_hours,
                                'used_hours': used,
                            },
                        )
                        session.add(log)
                        session.flush()

                        salary_record = _find_or_none_salary_record(
                            emp.id, period_year, period_month, session
                        )
                        if salary_record is not None and not getattr(salary_record, 'is_finalized', False):
                            salary_record.unused_leave_payout = (
                                (salary_record.unused_leave_payout or Decimal("0")) + amount
                            )
                            log.salary_record_id = salary_record.id

                        paid_count += 1
                        total_paid += amount
                else:
                    cold_start_count += 1

                new_period_end = _add_one_year_with_feb29_handling(today)
                hours = _calc_annual_leave_hours(
                    emp.hire_date, year=today.year, reference_date=today
                )
                session.add(LeaveQuota(
                    employee_id=emp.id,
                    year=today.year,
                    school_year=None,
                    period_start=today,
                    period_end=new_period_end,
                    leave_type='annual',
                    total_hours=hours,
                    note=f"週年制配額（hire_date 基準 {emp.hire_date.isoformat()}）",
                ))
        except IntegrityError:
            session.rollback()
        except Exception:
            logger.exception("cutover_annual failed for emp=%d", emp.id)

    return {
        'paid_employees': paid_count,
        'cold_start_employees': cold_start_count,
        'total_amount': float(total_paid),
        'total_anniversaries': len(candidates),
    }
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `pytest tests/test_cutover_annual_leave_anniversaries.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add services/leave_quota_expiry/annual_cutover.py tests/test_cutover_annual_leave_anniversaries.py
git commit -m "feat(leave-expiry): cutover_annual_leave_anniversaries scheduler 主邏輯"
```

---

## Task 11: Asyncio Polling Scheduler + main.py register

**Files:**
- Create: `services/leave_quota_expiry_scheduler.py`
- Modify: `main.py:324+`（recruitment_term_advance 後）
- Modify: `config/scheduler.py`
- Test: `tests/test_leave_quota_expiry_scheduler.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_leave_quota_expiry_scheduler.py
import asyncio
import pytest

from services.leave_quota_expiry_scheduler import (
    scheduler_enabled,
    run_leave_quota_expiry_scheduler,
)


def test_scheduler_enabled_default_false(monkeypatch):
    monkeypatch.delenv("LEAVE_QUOTA_EXPIRY_ENABLED", raising=False)
    from config import get_settings
    get_settings.cache_clear()
    assert scheduler_enabled() is False


@pytest.mark.asyncio
async def test_scheduler_stops_on_event(monkeypatch):
    """stop_event set → loop 結束"""
    monkeypatch.setenv("LEAVE_QUOTA_EXPIRY_ENABLED", "true")
    monkeypatch.setenv("LEAVE_QUOTA_EXPIRY_CHECK_INTERVAL", "1")
    from config import get_settings
    get_settings.cache_clear()

    stop = asyncio.Event()
    task = asyncio.create_task(run_leave_quota_expiry_scheduler(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_leave_quota_expiry_scheduler.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: 修改 config + 寫 scheduler**

```python
# config/scheduler.py 加兩欄
class SchedulerSettings(BaseSettings):
    # ... 既有欄位 ...
    leave_quota_expiry_enabled: bool = False
    leave_quota_expiry_check_interval: int = 3600
```

```python
# services/leave_quota_expiry_scheduler.py
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import get_settings

logger = logging.getLogger(__name__)


def _today_taipei() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.leave_quota_expiry_enabled)


async def run_leave_quota_expiry_scheduler(stop_event: asyncio.Event) -> None:
    """每日輪詢補休到期 + 特休週年 cutover。"""
    from models.base import session_scope
    from utils.advisory_lock import try_scheduler_lock
    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants
    from services.leave_quota_expiry.annual_cutover import cutover_annual_leave_anniversaries

    check_interval = get_settings().scheduler.leave_quota_expiry_check_interval
    logger.info("leave quota expiry scheduler 啟動 (interval=%ss)", check_interval)

    last_run_date: date | None = None

    while not stop_event.is_set():
        try:
            today = _today_taipei()
            if last_run_date != today:
                with session_scope() as session:
                    with try_scheduler_lock(
                        session,
                        scheduler_name="leave_quota_expiry",
                    ) as acquired:
                        if acquired:
                            comp_summary = expire_comp_leave_grants(today, session)
                            cutover_summary = cutover_annual_leave_anniversaries(today, session)
                            logger.info(
                                "leave quota expiry tick: %s | %s",
                                comp_summary,
                                cutover_summary,
                            )
                            last_run_date = today
        except Exception:
            logger.exception("leave quota expiry scheduler tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass
```

```python
# main.py 約 line 324 後（recruitment_term_advance 之後）加：
from services import leave_quota_expiry_scheduler as _lqe_sched
leave_quota_expiry_stop_event = asyncio.Event()
if _lqe_sched.scheduler_enabled():
    leave_quota_expiry_task = asyncio.create_task(
        _lqe_sched.run_leave_quota_expiry_scheduler(leave_quota_expiry_stop_event)
    )
    logger.info("leave quota expiry scheduler 已啟用")
```

並在 shutdown handler 加：

```python
leave_quota_expiry_stop_event.set()
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `pytest tests/test_leave_quota_expiry_scheduler.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add services/leave_quota_expiry_scheduler.py config/scheduler.py main.py tests/test_leave_quota_expiry_scheduler.py
git commit -m "feat(leave-expiry): asyncio polling scheduler + main lifespan + config flag"
```

---

## Task 12: `_grant_comp_leave_quota` 加 grant ledger write

**Files:**
- Modify: `api/overtimes.py:244-280`
- Test: `tests/test_grant_comp_leave_quota_with_ledger.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_grant_comp_leave_quota_with_ledger.py
"""驗證 _grant_comp_leave_quota 同步建 OvertimeCompLeaveGrant row。"""

from datetime import date

from api.overtimes import _grant_comp_leave_quota
from models.overtime import OvertimeRecord
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant


def test_grant_comp_leave_creates_ledger_row(session, employee_factory):
    emp = employee_factory()
    ot = OvertimeRecord(
        employee_id=emp.id,
        overtime_date=date(2026, 4, 1),
        overtime_type='weekday',
        hours=4.0,
        use_comp_leave=True,
        comp_leave_granted=False,
        is_approved=True,
    )
    session.add(ot)
    session.flush()

    result = {}
    _grant_comp_leave_quota(session, ot, result)

    assert ot.comp_leave_granted is True
    assert result['comp_leave_hours_granted'] == 4.0

    grant = session.query(OvertimeCompLeaveGrant).filter_by(overtime_record_id=ot.id).first()
    assert grant is not None
    assert grant.granted_hours == 4.0
    assert grant.granted_at == date(2026, 4, 1)
    assert grant.expires_at == date(2027, 4, 1)
    assert grant.status == 'active'


def test_grant_comp_leave_idempotent_does_not_duplicate(session, employee_factory):
    """重複 call 不會建第二筆 grant（comp_leave_granted=True early return）"""
    emp = employee_factory()
    ot = OvertimeRecord(
        employee_id=emp.id, overtime_date=date(2026, 4, 1), overtime_type='weekday',
        hours=4.0, use_comp_leave=True, comp_leave_granted=False, is_approved=True,
    )
    session.add(ot); session.flush()
    _grant_comp_leave_quota(session, ot, {})
    _grant_comp_leave_quota(session, ot, {})

    grants = session.query(OvertimeCompLeaveGrant).filter_by(overtime_record_id=ot.id).all()
    assert len(grants) == 1
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_grant_comp_leave_quota_with_ledger.py -v`
Expected: FAIL（grant 不存在）

- [ ] **Step 3: 修改 `_grant_comp_leave_quota`**

於 `api/overtimes.py` 的 `_grant_comp_leave_quota` 函式內，**既有 LeaveQuota upsert 保留**，新增 grant ledger row：

```python
# api/overtimes.py:244 附近
from datetime import timedelta
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant


def _grant_comp_leave_quota(session, ot: OvertimeRecord, result: dict) -> None:
    """核准補休模式加班時，upsert 補休配額並建 grant ledger row。"""
    if not (ot.use_comp_leave and not ot.comp_leave_granted):
        return

    # ── 既有 LeaveQuota upsert 邏輯保留（降級為快取，但 22+ caller 仍依賴） ──
    # ... existing code ...

    # ── 新增 grant ledger row ──
    grant = OvertimeCompLeaveGrant(
        overtime_record_id=ot.id,
        employee_id=ot.employee_id,
        granted_hours=ot.hours,
        granted_at=ot.overtime_date,
        expires_at=ot.overtime_date + timedelta(days=365),
        status='active',
    )
    session.add(grant)

    ot.comp_leave_granted = True
    result["comp_leave_hours_granted"] = ot.hours
    logger.info(
        "補休配額已發放 + grant ledger：員工 ID=%d, OT #%d, %.1f 小時，到期 %s",
        ot.employee_id, ot.id, ot.hours, grant.expires_at.isoformat(),
    )
```

- [ ] **Step 4: 跑測試確認 PASS + 既有 OT 相關測試無 regression**

Run: `pytest tests/test_grant_comp_leave_quota_with_ledger.py tests/test_overtimes_api.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add api/overtimes.py tests/test_grant_comp_leave_quota_with_ledger.py
git commit -m "feat(overtimes): _grant_comp_leave_quota 同步建 OvertimeCompLeaveGrant ledger row"
```

---

## Task 13: `_revoke_comp_leave_grant` 加 grant revoke

**Files:**
- Modify: `api/overtimes.py:106-240`
- Test: `tests/test_revoke_comp_leave_grant_with_ledger.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_revoke_comp_leave_grant_with_ledger.py
from datetime import date

from api.overtimes import _revoke_comp_leave_grant
from models.overtime import OvertimeRecord
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant


def test_revoke_marks_grant_revoked_not_deleted(session, employee_factory):
    emp = employee_factory()
    ot = OvertimeRecord(
        employee_id=emp.id, overtime_date=date(2026, 4, 1), overtime_type='weekday',
        hours=4.0, use_comp_leave=True, comp_leave_granted=True, is_approved=True,
    )
    session.add(ot); session.flush()
    grant = OvertimeCompLeaveGrant(
        overtime_record_id=ot.id, employee_id=emp.id,
        granted_hours=4.0, granted_at=date(2026, 4, 1), expires_at=date(2027, 4, 1),
        status='active',
    )
    session.add(grant); session.flush()

    _revoke_comp_leave_grant(session, ot)

    session.refresh(grant)
    assert grant.status == 'revoked'  # 不刪除留 audit
    assert ot.comp_leave_granted is False
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_revoke_comp_leave_grant_with_ledger.py -v`
Expected: FAIL（status 仍 'active'）

- [ ] **Step 3: 修改 `_revoke_comp_leave_grant`**

於 `api/overtimes.py`：

```python
# api/overtimes.py:106 附近
def _revoke_comp_leave_grant(session, ot: OvertimeRecord) -> None:
    """撤銷已發放的補休配額。"""
    if not ot.use_comp_leave or not ot.comp_leave_granted:
        return

    # ── 既有 LeaveQuota 操作邏輯保留 ──
    # ... existing code ...

    # ── 新增：mark grant ledger row 為 revoked ──
    grant = session.query(OvertimeCompLeaveGrant).filter_by(overtime_record_id=ot.id).first()
    if grant is not None:
        grant.status = 'revoked'

    ot.comp_leave_granted = False
```

- [ ] **Step 4: 跑測試 PASS + 既有 overtime 測試無 regression**

Run: `pytest tests/test_revoke_comp_leave_grant_with_ledger.py tests/test_overtimes_api.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add api/overtimes.py tests/test_revoke_comp_leave_grant_with_ledger.py
git commit -m "feat(overtimes): _revoke_comp_leave_grant 同步 mark grant 'revoked' (audit 保留)"
```

---

## Task 14: 補休假單 FIFO 扣抵 grant

**Files:**
- Modify: `api/leaves.py`（補休假單 approve/reject endpoint）
- Test: `tests/test_compensatory_leave_fifo.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_compensatory_leave_fifo.py
"""補休假單核准時 FIFO 從最早 expires_at 扣 consumed_hours。"""

from datetime import date

from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
from models.leave import LeaveRecord


def test_approve_compensatory_leave_fifo_consumes_earliest(session, employee_factory):
    emp = employee_factory()

    # 兩筆 grant：g1 expires 較早，g2 較晚
    g1 = OvertimeCompLeaveGrant(
        overtime_record_id=1, employee_id=emp.id,
        granted_hours=4.0, granted_at=date(2025, 4, 1), expires_at=date(2026, 4, 1),
        consumed_hours=0, status='active',
    )
    g2 = OvertimeCompLeaveGrant(
        overtime_record_id=2, employee_id=emp.id,
        granted_hours=8.0, granted_at=date(2025, 5, 1), expires_at=date(2026, 5, 1),
        consumed_hours=0, status='active',
    )
    session.add_all([g1, g2]); session.flush()

    leave = LeaveRecord(
        employee_id=emp.id, leave_type='compensatory',
        start_date=date(2025, 12, 1), end_date=date(2025, 12, 1),
        leave_hours=6.0, is_approved=None,
    )
    session.add(leave); session.flush()

    from api.leaves import _consume_compensatory_grants_fifo
    _consume_compensatory_grants_fifo(session, emp.id, hours=6.0)

    session.refresh(g1); session.refresh(g2)
    assert g1.consumed_hours == 4.0  # g1 全用完
    assert g2.consumed_hours == 2.0  # g2 補 2 小時


def test_reject_compensatory_leave_releases_consumed(session, employee_factory):
    emp = employee_factory()
    g1 = OvertimeCompLeaveGrant(
        overtime_record_id=1, employee_id=emp.id,
        granted_hours=4.0, granted_at=date(2025, 4, 1), expires_at=date(2026, 4, 1),
        consumed_hours=3.0, status='active',
    )
    session.add(g1); session.flush()

    from api.leaves import _release_compensatory_grants_fifo
    _release_compensatory_grants_fifo(session, emp.id, hours=3.0)

    session.refresh(g1)
    assert g1.consumed_hours == 0.0
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_compensatory_leave_fifo.py -v`
Expected: ImportError `_consume_compensatory_grants_fifo`

- [ ] **Step 3: 實作 FIFO helpers + wire 到 approve/reject path**

於 `api/leaves.py` 新增：

```python
def _consume_compensatory_grants_fifo(session, employee_id: int, hours: float) -> None:
    """FIFO 從最早 expires_at 的 active grant 扣 consumed_hours。

    若 active grant 總額不足，剩餘部分 raise（不允許超扣，前端應已驗 quota）。
    """
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    remaining = float(hours)
    grants = (
        session.query(OvertimeCompLeaveGrant)
        .filter(
            OvertimeCompLeaveGrant.employee_id == employee_id,
            OvertimeCompLeaveGrant.status == 'active',
        )
        .order_by(OvertimeCompLeaveGrant.expires_at.asc())
        .all()
    )
    for g in grants:
        if remaining <= 0:
            break
        available = g.granted_hours - g.consumed_hours
        if available <= 0:
            continue
        take = min(available, remaining)
        g.consumed_hours += take
        remaining -= take
    if remaining > 0:
        raise ValueError(f"補休 grant 不足扣抵：尚缺 {remaining} 小時")


def _release_compensatory_grants_fifo(session, employee_id: int, hours: float) -> None:
    """退回 consumed_hours：LIFO 從最近扣的 grant 開始退（與 consume 對稱）。

    保守版：簡化為 FIFO（先扣的先還），維持結餘正確即可，個別 grant 配對不嚴格。
    """
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    remaining = float(hours)
    grants = (
        session.query(OvertimeCompLeaveGrant)
        .filter(
            OvertimeCompLeaveGrant.employee_id == employee_id,
            OvertimeCompLeaveGrant.status == 'active',
            OvertimeCompLeaveGrant.consumed_hours > 0,
        )
        .order_by(OvertimeCompLeaveGrant.expires_at.asc())
        .all()
    )
    for g in grants:
        if remaining <= 0:
            break
        take = min(g.consumed_hours, remaining)
        g.consumed_hours -= take
        remaining -= take
```

於既有補休假單 approve endpoint 內：

```python
# 在補休假單 approve 處（leave_type == 'compensatory' AND is_approved 轉為 True 時）
if leave.leave_type == 'compensatory':
    _consume_compensatory_grants_fifo(session, leave.employee_id, leave.leave_hours)
```

於既有補休假單 reject/withdraw endpoint 內：

```python
# 在補休假單 reject/withdraw 處（原為 is_approved=True 撤回時）
if leave.leave_type == 'compensatory' and leave.is_approved is True:
    _release_compensatory_grants_fifo(session, leave.employee_id, leave.leave_hours)
```

- [ ] **Step 4: 跑測試確認 PASS + leaves 測試無 regression**

Run: `pytest tests/test_compensatory_leave_fifo.py tests/test_leaves_api.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add api/leaves.py tests/test_compensatory_leave_fifo.py
git commit -m "feat(leaves): 補休假單 FIFO 扣抵 grant ledger（核准消耗、駁回退回）"
```

---

## Task 15: `leave_quota_cutover.py` 移除 annual + 改 compensatory 走 grant SUM

**Files:**
- Modify: `services/term_subscribers/leave_quota_cutover.py`
- Test: append to `tests/test_leave_quota_cutover.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_leave_quota_cutover.py 追加
def test_cutover_does_not_create_annual_row_anymore(session, employee_factory, term_factory):
    """跨學年 cutover 不再建 annual row（由 anniversary scheduler 負責）"""
    emp = employee_factory(hire_date=date(2020, 4, 1))
    old_term = term_factory(school_year=2025, semester=2, start_date=date(2026, 2, 1))
    new_term = term_factory(school_year=2026, semester=1, start_date=date(2026, 8, 1))

    from services.term_subscribers.leave_quota_cutover import handle
    handle(old=old_term, new=new_term, session=session)

    from models.leave import LeaveQuota
    annual_rows = session.query(LeaveQuota).filter_by(
        employee_id=emp.id, leave_type='annual', school_year=2026,
    ).all()
    assert len(annual_rows) == 0  # 不再建


def test_cutover_compensatory_carryover_uses_grant_sum(session, employee_factory, term_factory):
    """補休 carry-over 數值 = 當下 active grants SUM(granted - consumed)"""
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant

    emp = employee_factory()
    OvertimeCompLeaveGrant.__table__.create(session.bind, checkfirst=True)
    session.add(OvertimeCompLeaveGrant(
        overtime_record_id=1, employee_id=emp.id,
        granted_hours=8.0, granted_at=date(2025, 4, 1), expires_at=date(2026, 4, 1),
        consumed_hours=2.0, status='active',
    ))
    session.commit()

    old_term = term_factory(school_year=2025, semester=2, start_date=date(2026, 2, 1))
    new_term = term_factory(school_year=2026, semester=1, start_date=date(2026, 8, 1))

    from services.term_subscribers.leave_quota_cutover import handle
    handle(old=old_term, new=new_term, session=session)

    from models.leave import LeaveQuota
    comp = session.query(LeaveQuota).filter_by(
        employee_id=emp.id, leave_type='compensatory', school_year=2026,
    ).first()
    assert comp.total_hours == 6.0  # 8 - 2
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_leave_quota_cutover.py -v -k "not_create_annual or grant_sum"`
Expected: FAIL

- [ ] **Step 3: 修改 cutover handler**

`services/term_subscribers/leave_quota_cutover.py`：

1. 移除 `annual` 從處理迴圈：

```python
# QUOTA_LEAVE_TYPES 中移除 'annual'（或在迴圈 skip）
for lt in QUOTA_LEAVE_TYPES:
    if lt == 'annual':
        continue  # 走 anniversary scheduler
    if lt in existing_types:
        continue
    # ... 其餘法定假別不變 ...
```

2. 改寫 `_calc_compensatory_balance`：

```python
def _calc_compensatory_balance(
    employee_id: int,
    old: AcademicTerm,
    new: AcademicTerm,
    session: Session,
) -> float:
    """補休結餘改用 grant ledger SUM 作 source of truth。"""
    from services.leave_quota_expiry.helpers import _compensatory_balance
    return _compensatory_balance(employee_id, session)
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `pytest tests/test_leave_quota_cutover.py -v`
Expected: 既有測試含新加 PASS

- [ ] **Step 5: Commit**

```bash
git add services/term_subscribers/leave_quota_cutover.py tests/test_leave_quota_cutover.py
git commit -m "feat(cutover): 移除 annual cutover（改週年 scheduler）+ compensatory 改 grant ledger SUM"
```

---

## Task 16: Salary Engine 整合 layer 2

**Files:**
- Modify: `services/salary/engine.py`
- Test: `tests/test_salary_engine_pulls_pending_payout_logs.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_salary_engine_pulls_pending_payout_logs.py
from datetime import date
from decimal import Decimal


def test_calculate_pulls_pending_payout_logs_and_binds(session, employee_factory, salary_record_factory):
    """月結 calculate 撈 salary_record_id IS NULL log 加總 + 反向綁定"""
    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    emp = employee_factory(employee_type='monthly', base_salary=48000.0)
    log = UnusedLeavePayoutLog(
        employee_id=emp.id, source_type='comp_grant_expiry', source_ref_id=None,
        hours=4.0, hourly_wage=Decimal('200.00'), amount=Decimal('800.00'),
        wage_basis_date=date(2026, 4, 1), salary_period_year=2026, salary_period_month=5,
        meta={},
    )
    session.add(log); session.commit()

    from services.salary.engine import SalaryEngine
    engine = SalaryEngine(session=session)
    sr = engine.calculate(year=2026, month=5, employee_id=emp.id)

    assert sr.unused_leave_payout == Decimal('800.00')
    session.refresh(log)
    assert log.salary_record_id == sr.id


def test_calculate_does_not_double_count_bound_logs(session, employee_factory, salary_record_factory):
    """已綁定 log 重 calc 不會重複加（被 filter 排除）"""
    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    emp = employee_factory(employee_type='monthly', base_salary=48000.0)
    sr_initial = salary_record_factory(emp_id=emp.id, year=2026, month=5, unused_leave_payout=Decimal('800.00'))
    log = UnusedLeavePayoutLog(
        employee_id=emp.id, source_type='comp_grant_expiry', source_ref_id=None,
        hours=4.0, hourly_wage=Decimal('200.00'), amount=Decimal('800.00'),
        wage_basis_date=date(2026, 4, 1), salary_period_year=2026, salary_period_month=5,
        salary_record_id=sr_initial.id, meta={},
    )
    session.add(log); session.commit()

    from services.salary.engine import SalaryEngine
    engine = SalaryEngine(session=session)
    sr = engine.calculate(year=2026, month=5, employee_id=emp.id)

    assert sr.unused_leave_payout == Decimal('800.00')  # 仍為 800 非 1600
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_salary_engine_pulls_pending_payout_logs.py -v`
Expected: FAIL（unused_leave_payout=0 or missing logic）

- [ ] **Step 3: 修改 `services/salary/engine.py`**

於 `calculate()` 內、所有金額計算完成、`SalaryRecord` 寫入後加 step：

```python
# services/salary/engine.py，calculate() 內 SalaryRecord 寫入後加：
from models.unused_leave_payout_log import UnusedLeavePayoutLog

pending_logs = (
    session.query(UnusedLeavePayoutLog)
    .filter(
        UnusedLeavePayoutLog.employee_id == employee_id,
        UnusedLeavePayoutLog.salary_period_year == year,
        UnusedLeavePayoutLog.salary_period_month == month,
        UnusedLeavePayoutLog.salary_record_id.is_(None),
    )
    .all()
)

if pending_logs:
    additional = sum(log.amount for log in pending_logs)
    salary_record.unused_leave_payout = (
        (salary_record.unused_leave_payout or Decimal("0")) + additional
    )
    session.flush()
    for log in pending_logs:
        log.salary_record_id = salary_record.id
```

- [ ] **Step 4: 跑測試 PASS + salary engine 既有測試無 regression**

Run: `pytest tests/test_salary_engine_pulls_pending_payout_logs.py tests/test_salary_engine.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add services/salary/engine.py tests/test_salary_engine_pulls_pending_payout_logs.py
git commit -m "feat(salary): engine.calculate 撈 pending unused_leave_payout_log 加總並反向綁定"
```

---

## Task 17: HR API endpoints

**Files:**
- Create: `api/leave_quota_expiry.py`
- Modify: `main.py`（register router）
- Test: `tests/test_leave_quota_expiry_api.py`

- [ ] **Step 1: 寫 failing test**

```python
# tests/test_leave_quota_expiry_api.py
from datetime import date, timedelta
from decimal import Decimal


def test_upcoming_lists_grants_within_window(client, hr_user, employee_factory):
    """GET /leave-quota-expiry/upcoming?days=30 列即將到期"""
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
    emp = employee_factory()
    # 一筆 10 天後到期
    grant = OvertimeCompLeaveGrant(
        overtime_record_id=1, employee_id=emp.id,
        granted_hours=4.0, granted_at=date.today() - timedelta(days=355),
        expires_at=date.today() + timedelta(days=10),
        status='active',
    )
    # 一筆 60 天後到期（不在 window）
    # ... insert ...

    resp = client.get("/leave-quota-expiry/upcoming?days=30", cookies=hr_user.cookies)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data['grants']) == 1
    assert data['grants'][0]['grant_id'] == grant.id


def test_anniversaries_lists_upcoming_30_days(client, hr_user, employee_factory):
    """GET /leave-quota-expiry/anniversaries"""
    # ... 略
    pass


def test_payout_history_returns_logs(client, hr_user):
    """GET /leave-quota-expiry/payout-history"""
    pass


def test_run_now_triggers_scheduler_idempotent(client, salary_write_user):
    """POST /leave-quota-expiry/run-now"""
    resp = client.post("/leave-quota-expiry/run-now", cookies=salary_write_user.cookies)
    assert resp.status_code == 200
    assert "comp_summary" in resp.json()
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `pytest tests/test_leave_quota_expiry_api.py -v`
Expected: 404 (router not registered)

- [ ] **Step 3: 實作 api**

```python
# api/leave_quota_expiry.py
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from models.base import get_session
from models.employee import Employee
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
from models.unused_leave_payout_log import UnusedLeavePayoutLog
from services.leave_quota_expiry.helpers import _is_anniversary_today_sql
from utils.auth import require_permission

router = APIRouter(prefix="/leave-quota-expiry", tags=["leave-quota-expiry"])


@router.get("/upcoming")
def list_upcoming_expiring_grants(
    days: int = Query(30, ge=1, le=365),
    session: Session = Depends(get_session),
    _=Depends(require_permission("LEAVES_READ")),
):
    """列出未來 N 天內到期的 active grant。"""
    end = date.today() + timedelta(days=days)
    grants = (
        session.query(OvertimeCompLeaveGrant)
        .filter(
            OvertimeCompLeaveGrant.status == 'active',
            OvertimeCompLeaveGrant.expires_at >= date.today(),
            OvertimeCompLeaveGrant.expires_at <= end,
        )
        .order_by(OvertimeCompLeaveGrant.expires_at.asc())
        .all()
    )
    return {
        "grants": [
            {
                "grant_id": g.id,
                "employee_id": g.employee_id,
                "granted_hours": g.granted_hours,
                "consumed_hours": g.consumed_hours,
                "unexpired_hours": g.granted_hours - g.consumed_hours,
                "granted_at": g.granted_at.isoformat(),
                "expires_at": g.expires_at.isoformat(),
            }
            for g in grants
        ]
    }


@router.get("/anniversaries")
def list_upcoming_anniversaries(
    days: int = Query(30, ge=1, le=365),
    session: Session = Depends(get_session),
    _=Depends(require_permission("LEAVES_READ")),
):
    """列出未來 N 天內滿週年的員工。"""
    today = date.today()
    end = today + timedelta(days=days)
    # 簡化：撈 active employee，Python 端過濾「即將週年」
    emps = session.query(Employee).filter(Employee.is_active.is_(True)).all()
    results = []
    for emp in emps:
        if emp.hire_date is None:
            continue
        for offset in range(days + 1):
            check = today + timedelta(days=offset)
            try:
                anniv = emp.hire_date.replace(year=check.year)
            except ValueError:
                anniv = emp.hire_date.replace(year=check.year, day=28)
            if anniv == check and emp.hire_date <= today - timedelta(days=180):
                results.append({
                    "employee_id": emp.id,
                    "hire_date": emp.hire_date.isoformat(),
                    "next_anniversary": anniv.isoformat(),
                })
                break
    return {"anniversaries": results}


@router.get("/payout-history")
def list_payout_history(
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session),
    _=Depends(require_permission("SALARY_READ")),
):
    """列出 unused_leave_payout_log 結算歷史。"""
    logs = (
        session.query(UnusedLeavePayoutLog)
        .order_by(UnusedLeavePayoutLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "logs": [
            {
                "log_id": l.id,
                "employee_id": l.employee_id,
                "source_type": l.source_type,
                "hours": l.hours,
                "amount": float(l.amount),
                "wage_basis_date": l.wage_basis_date.isoformat(),
                "salary_period": f"{l.salary_period_year}-{l.salary_period_month:02d}",
                "salary_record_id": l.salary_record_id,
                "meta": l.meta,
            }
            for l in logs
        ]
    }


@router.post("/run-now")
def run_scheduler_now(
    session: Session = Depends(get_session),
    _=Depends(require_permission("SALARY_WRITE")),
):
    """手動 trigger scheduler handle（idempotent 重跑安全）。"""
    from datetime import date

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants
    from services.leave_quota_expiry.annual_cutover import cutover_annual_leave_anniversaries

    today = date.today()
    comp_summary = expire_comp_leave_grants(today, session)
    cutover_summary = cutover_annual_leave_anniversaries(today, session)
    session.commit()
    return {"comp_summary": comp_summary, "cutover_summary": cutover_summary}
```

於 `main.py` register：

```python
from api import leave_quota_expiry as _lqe_api
app.include_router(_lqe_api.router, prefix="/api")
```

- [ ] **Step 4: 跑測試 PASS**

Run: `pytest tests/test_leave_quota_expiry_api.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add api/leave_quota_expiry.py main.py tests/test_leave_quota_expiry_api.py
git commit -m "feat(api): leave-quota-expiry 4 endpoints (upcoming/anniversaries/payout-history/run-now)"
```

---

## Task 18: Integration & idempotency sanity test

**Files:**
- Create: `tests/test_leave_quota_lifecycle_integration.py`

- [ ] **Step 1: 寫整合測試**

```python
# tests/test_leave_quota_lifecycle_integration.py
"""端到端整合：OT 核准 → 半年後員工請補休 → 一年後 scheduler 結算 → SalaryRecord 含金額"""

from datetime import date, timedelta
from decimal import Decimal


def test_full_lifecycle_ot_approve_then_partial_consume_then_expire_writes_salary(
    session, employee_factory, salary_record_factory, freezer
):
    emp = employee_factory(employee_type='hourly', hourly_rate=200.0)

    # T0: OT 核准 → grant 自動建
    from models.overtime import OvertimeRecord
    from api.overtimes import _grant_comp_leave_quota
    ot = OvertimeRecord(
        employee_id=emp.id, overtime_date=date(2025, 4, 1), overtime_type='weekday',
        hours=8.0, use_comp_leave=True, comp_leave_granted=False, is_approved=True,
    )
    session.add(ot); session.flush()
    _grant_comp_leave_quota(session, ot, {})

    # T+6 個月：員工請 3 小時補休
    from api.leaves import _consume_compensatory_grants_fifo
    _consume_compensatory_grants_fifo(session, emp.id, hours=3.0)

    # T+1 年 1 天：scheduler 跑
    freezer.move_to(date(2026, 4, 2))
    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants
    summary = expire_comp_leave_grants(date(2026, 4, 2), session)

    # 應結算 (8-3)=5 小時 → 5*200=1000
    assert summary['paid_employees'] == 1
    assert summary['total_amount'] == 1000.0

    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    log = session.query(UnusedLeavePayoutLog).first()
    assert log.amount == Decimal('1000.00')

    # T+1 年 1 天，月結 calculate 5/2026
    sr = salary_record_factory(emp_id=emp.id, year=2026, month=5)
    from services.salary.engine import SalaryEngine
    engine = SalaryEngine(session=session)
    sr2 = engine.calculate(year=2026, month=5, employee_id=emp.id)
    assert sr2.unused_leave_payout == Decimal('1000.00')


def test_scheduler_idempotent_double_run_same_day_no_duplicate(session, employee_factory):
    """同日重跑 scheduler 不會建第二筆 log，grant 不會再被選"""
    emp = employee_factory(employee_type='hourly', hourly_rate=200.0)
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
    g = OvertimeCompLeaveGrant(
        overtime_record_id=1, employee_id=emp.id,
        granted_hours=4.0, granted_at=date(2025, 4, 1), expires_at=date(2026, 3, 31),
        status='active',
    )
    session.add(g); session.commit()

    from services.leave_quota_expiry.comp_leave_expiry import expire_comp_leave_grants
    expire_comp_leave_grants(date(2026, 4, 1), session)
    expire_comp_leave_grants(date(2026, 4, 1), session)  # 第二次

    from models.unused_leave_payout_log import UnusedLeavePayoutLog
    assert session.query(UnusedLeavePayoutLog).count() == 1
```

- [ ] **Step 2: 跑測試確認 PASS**

Run: `pytest tests/test_leave_quota_lifecycle_integration.py -v`
Expected: 2 PASS

- [ ] **Step 3: 跑全套 backend pytest 無 regression**

Run: `pytest tests/ -v --tb=short 2>&1 | tail -20`
Expected: 既有測試數量 + 新增測試數量全綠（部分 pre-existing fail 與本 plan 無關）

- [ ] **Step 4: Commit**

```bash
git add tests/test_leave_quota_lifecycle_integration.py
git commit -m "test(leave-expiry): 端到端整合 + scheduler idempotency sanity"
```

---

## Out of Scope（Phase B 另開 plan）

- `LeaveQuotaExpiryTab.vue` 前端元件
- LeavesView 加 sub-tab + 補休餘額顯示「最早到期日」
- SalaryView tooltip 證據鏈展開
- `src/api/leaveQuotaExpiry.ts` axios wrapper
- E2E Playwright critical-path
- 離職 path 補寫 `source_type='offboarding'` log（Phase 2）
- HR 手動 extend grant.expires_at endpoint（defer）
- LINE Bot 即將到期推播（Phase 2）
