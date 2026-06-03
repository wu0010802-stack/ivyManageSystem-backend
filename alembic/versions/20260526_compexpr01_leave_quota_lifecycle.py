"""leave_quota_lifecycle: unused_leave_payout_log + overtime_comp_leave_grants + LeaveQuota period 欄

Revision ID: compexpr01
Revises: mergeheads04
Create Date: 2026-05-26 00:00:00.000000

升級包含：
1. CREATE TABLE unused_leave_payout_log（含 3 indexes）
2. CREATE TABLE overtime_comp_leave_grants（含 2 indexes + CheckConstraint + FK 到 unused_leave_payout_log）
3. ALTER TABLE leave_quotas 加 period_start / period_end + partial unique index
4. Backfill 既有 OT（use_comp_leave=True AND comp_leave_granted=True AND is_approved=True）→ grant rows
5. Backfill 既有 annual LeaveQuota（period_start IS NULL）→ 設 period_start / period_end

降級：對稱 drop（不嘗試 reverse backfill）
"""

import os
from datetime import date, timedelta

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "compexpr01"
down_revision = "mergeheads04"
branch_labels = None
depends_on = None

BACKFILL_GRACE_MONTHS = int(os.environ.get("LEAVE_BACKFILL_GRACE_MONTHS", "3"))


def upgrade():
    # 1. unused_leave_payout_log
    op.create_table(
        "unused_leave_payout_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "employee_id",
            sa.Integer,
            sa.ForeignKey("employees.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_ref_id", sa.Integer, nullable=True),
        sa.Column("hours", sa.Float, nullable=False),
        sa.Column("hourly_wage", sa.Numeric(10, 2), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("wage_basis_date", sa.Date, nullable=False),
        sa.Column(
            "salary_record_id",
            sa.Integer,
            sa.ForeignKey("salary_records.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("salary_period_year", sa.Integer, nullable=False),
        sa.Column("salary_period_month", sa.Integer, nullable=False),
        sa.Column(
            "meta",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_payout_log_emp_period",
        "unused_leave_payout_log",
        ["employee_id", "salary_period_year", "salary_period_month"],
    )
    op.create_index(
        "ix_payout_log_salary_record",
        "unused_leave_payout_log",
        ["salary_record_id"],
        postgresql_where=sa.text("salary_record_id IS NOT NULL"),
    )
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
        sa.Column(
            "overtime_record_id",
            sa.Integer,
            sa.ForeignKey("overtime_records.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "employee_id",
            sa.Integer,
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("granted_hours", sa.Float, nullable=False),
        sa.Column("granted_at", sa.Date, nullable=False),
        sa.Column("expires_at", sa.Date, nullable=False),
        sa.Column(
            "consumed_hours",
            sa.Float,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="active",
        ),
        sa.Column("expired_at", sa.DateTime, nullable=True),
        sa.Column(
            "payout_salary_record_id",
            sa.Integer,
            sa.ForeignKey("salary_records.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "payout_log_id",
            sa.BigInteger,
            sa.ForeignKey("unused_leave_payout_log.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "consumed_hours <= granted_hours",
            name="ck_grant_consumed_le_granted",
        ),
    )
    op.create_index(
        "ix_grant_emp_status_expires",
        "overtime_comp_leave_grants",
        ["employee_id", "status", "expires_at"],
    )
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
    op.execute(sa.text("""INSERT INTO overtime_comp_leave_grants (
                overtime_record_id, employee_id, granted_hours, granted_at, expires_at,
                consumed_hours, status
            )
            SELECT id, employee_id, hours, overtime_date, :grace_date, 0, 'active'
            FROM overtime_records
            WHERE use_comp_leave = TRUE
              AND comp_leave_granted = TRUE
              AND is_approved = TRUE
            """).bindparams(grace_date=grace_expires_at))

    # 5. Backfill 既有 annual LeaveQuota → period_start = hire_date 最近過去週年
    # 用 Python 端跑（SQL 表達 anniversary date 跨方言複雜，資料量小）
    bind = op.get_bind()
    rows = bind.execute(sa.text("""SELECT lq.id, lq.employee_id, lq.year, e.hire_date
               FROM leave_quotas lq
               JOIN employees e ON e.id = lq.employee_id
               WHERE lq.leave_type = 'annual' AND lq.period_start IS NULL
            """)).fetchall()
    # 同一員工可能有多筆 annual row（每西元年各一筆，school_year=NULL）；period_start
    # 由 hire_date 週年推得、與 row.year 無關 → 若每筆都填會得到「相同」period_start，
    # 撞 partial unique index uq_leave_quotas_emp_period_annual（key 不含 year）。
    # 因此每位員工只讓「最新年度」那筆 row 拿到非 NULL period_start，其餘維持 NULL
    # （partial index WHERE period_start IS NOT NULL 會自動忽略 NULL row）。
    canonical_by_emp = {}
    for lq_id, emp_id, yr, hire_date in rows:
        if hire_date is None:
            continue
        sort_key = (yr if yr is not None else -1, lq_id)
        prev = canonical_by_emp.get(emp_id)
        if prev is None or sort_key > prev[0]:
            canonical_by_emp[emp_id] = (sort_key, lq_id, hire_date)
    for _sort_key, lq_id, hire_date in canonical_by_emp.values():
        if hire_date is None:
            continue
        # hire_date 可能從 DB 拿到 str（SQLite）或 date 物件（Postgres）
        if isinstance(hire_date, str):
            hire_date = date.fromisoformat(hire_date)
        # 計算 hire_date 最近過去的週年
        years_elapsed = today.year - hire_date.year
        if (today.month, today.day) < (hire_date.month, hire_date.day):
            years_elapsed -= 1
        if years_elapsed < 0:
            continue  # 未到第一週年，不 backfill
        try:
            period_start = hire_date.replace(year=hire_date.year + years_elapsed)
        except ValueError:  # 2/29 → 非閏年
            period_start = hire_date.replace(
                year=hire_date.year + years_elapsed, day=28
            )
        try:
            period_end = period_start.replace(year=period_start.year + 1)
        except ValueError:
            period_end = period_start.replace(year=period_start.year + 1, day=28)
        bind.execute(
            sa.text(
                "UPDATE leave_quotas SET period_start = :ps, period_end = :pe WHERE id = :id"
            ).bindparams(ps=period_start, pe=period_end, id=lq_id)
        )


def downgrade():
    op.drop_index("uq_leave_quotas_emp_period_annual", table_name="leave_quotas")
    op.drop_column("leave_quotas", "period_end")
    op.drop_column("leave_quotas", "period_start")
    op.drop_index(
        "ix_grant_status_expires_active", table_name="overtime_comp_leave_grants"
    )
    op.drop_index(
        "ix_grant_emp_status_expires", table_name="overtime_comp_leave_grants"
    )
    op.drop_table("overtime_comp_leave_grants")
    op.drop_index("uq_payout_log_anniversary", table_name="unused_leave_payout_log")
    op.drop_index("ix_payout_log_salary_record", table_name="unused_leave_payout_log")
    op.drop_index("ix_payout_log_emp_period", table_name="unused_leave_payout_log")
    op.drop_table("unused_leave_payout_log")
