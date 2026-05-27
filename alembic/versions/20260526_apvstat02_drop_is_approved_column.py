"""drop_is_approved_column

Phase 4 of the approval-status enum rollout. Drops the legacy
`is_approved Boolean` column from leave_records / overtime_records /
punch_correction_requests, plus 6 is_approved-prefixed indexes
(3 on leave_records, 2 on overtime_records, 1 on
punch_correction_requests). Also adds a `(status, attendance_date)`
mirror index on punch_correction_requests that apvstat01 missed.
The `status` column added in apvstat01 is now the canonical source
of truth.

Downgrade re-creates the column nullable, backfills from `status` via
a frozen inverse mapping, re-adds the 6 is_approved indexes, then drops
the punch_correction status-mirror added by this migration. Frozen mapping
matches apvstat01:
    'approved' → TRUE
    'rejected' → FALSE
    else       → NULL

Revision ID: apvstat02
Revises: apvstat01
Create Date: 2026-05-26
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "apvstat02"
down_revision = "apvstat01"
branch_labels = None
depends_on = None

_TABLES = ("leave_records", "overtime_records", "punch_correction_requests")

_OLD_INDEXES = [
    # (table, index_name, columns) — columns only used by downgrade re-create.
    ("leave_records", "ix_leave_emp_approved", ["employee_id", "is_approved"]),
    ("leave_records", "ix_leave_approved_start_date", ["is_approved", "start_date"]),
    (
        "leave_records",
        "ix_leave_emp_type_approved",
        ["employee_id", "leave_type", "is_approved"],
    ),
    ("overtime_records", "ix_overtime_emp_approved", ["employee_id", "is_approved"]),
    (
        "overtime_records",
        "ix_overtime_approved_date",
        ["is_approved", "overtime_date"],
    ),
    (
        "punch_correction_requests",
        "ix_punch_correction_approval",
        ["is_approved", "attendance_date"],
    ),
]

# New status-prefixed index for punch_correction_requests — apvstat01 missed
# this; we add it here so the new status query path (api/punch_corrections.py
# filters PunchCorrectionRequest.status with attendance_date range) keeps the
# index scan that ix_punch_correction_approval used to provide.
_NEW_INDEXES = [
    (
        "punch_correction_requests",
        "ix_punch_correction_status",
        ["status", "attendance_date"],
    ),
]

_REVERSE_BACKFILL_SQL = """
UPDATE {table}
SET is_approved = CASE
    WHEN status = 'approved' THEN TRUE
    WHEN status = 'rejected' THEN FALSE
    ELSE NULL
END
"""


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # 1) Add new status-prefixed indexes BEFORE dropping is_approved indexes,
    #    so query plans never lose coverage during the migration.
    for table, name, cols in _NEW_INDEXES:
        existing = _existing_indexes(bind, table)
        if name not in existing:
            op.create_index(name, table, cols)

    # 2) Drop old is_approved indexes (idempotent — skip if already missing).
    for table, name, _cols in _OLD_INDEXES:
        existing = _existing_indexes(bind, table)
        if name in existing:
            op.drop_index(name, table_name=table)

    # 3) Drop the is_approved column from each table.
    for table in _TABLES:
        op.drop_column(table, "is_approved")


def downgrade() -> None:
    bind = op.get_bind()

    # 1) Re-add is_approved column (nullable).
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "is_approved",
                sa.Boolean(),
                nullable=True,
                comment="是否核准 (None=待審核, True=核准, False=駁回)",
            ),
        )

    # 2) Backfill from status.
    for table in _TABLES:
        op.execute(sa.text(_REVERSE_BACKFILL_SQL.format(table=table)))

    # 3) Re-add old is_approved indexes (idempotent).
    for table, name, cols in _OLD_INDEXES:
        existing = _existing_indexes(bind, table)
        if name not in existing:
            op.create_index(name, table, cols)

    # 4) Drop the new status-prefixed indexes.
    for table, name, _cols in _NEW_INDEXES:
        existing = _existing_indexes(bind, table)
        if name in existing:
            op.drop_index(name, table_name=table)
