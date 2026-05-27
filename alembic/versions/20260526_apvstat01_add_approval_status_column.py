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
    (
        "leave_records",
        "ix_leave_emp_type_status",
        ["employee_id", "leave_type", "status"],
    ),
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
