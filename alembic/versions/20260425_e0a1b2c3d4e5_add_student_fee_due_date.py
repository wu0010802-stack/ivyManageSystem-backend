"""add student_fee_records.due_date

家長入口 Batch 6：個別學生可有不同到期日；家長端做「即將到期/已逾期」分類。

Revision ID: e0a1b2c3d4e5
Revises: d9z0a1b2c3d4
Create Date: 2026-04-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "e0a1b2c3d4e5"
down_revision = "d9z0a1b2c3d4"
branch_labels = None
depends_on = None


def _column_names(bind, table: str) -> set:
    if table not in inspect(bind).get_table_names():
        return set()
    return {c["name"] for c in inspect(bind).get_columns(table)}


def _index_names(bind, table: str) -> set:
    if table not in inspect(bind).get_table_names():
        return set()
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "student_fee_records" not in inspect(bind).get_table_names():
        return
    cols = _column_names(bind, "student_fee_records")
    if "due_date" not in cols:
        op.add_column(
            "student_fee_records",
            sa.Column("due_date", sa.Date, nullable=True),
        )
    if "ix_fee_records_due_date" not in _index_names(bind, "student_fee_records"):
        op.create_index(
            "ix_fee_records_due_date", "student_fee_records", ["due_date"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "student_fee_records" not in inspect(bind).get_table_names():
        return
    if "ix_fee_records_due_date" in _index_names(bind, "student_fee_records"):
        op.drop_index("ix_fee_records_due_date", table_name="student_fee_records")
    if "due_date" in _column_names(bind, "student_fee_records"):
        op.drop_column("student_fee_records", "due_date")
