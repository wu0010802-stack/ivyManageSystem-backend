"""add index on leave_records(is_approved, start_date)

管理端查詢「待審核 / 已核准 / 已駁回」請假記錄時，WHERE 條件為
  is_approved IS NULL / = TRUE / = FALSE
且無 employee_id 限制，現有的 ix_leave_emp_dates(employee_id, start_date, end_date)
無法加速此類查詢，導致全表掃描。

新增 (is_approved, start_date) 複合索引，讓管理端按審核狀態列表時走 index scan。

Revision ID: k7l8m9n0o1p2
Revises: j6k7l8m9n0o1
Create Date: 2026-03-18 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "k7l8m9n0o1p2"
down_revision = "j6k7l8m9n0o1"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _existing_indexes(bind, "leave_records")

    # 管理端按審核狀態 + 日期排序的查詢加速
    # 例：WHERE is_approved IS NULL ORDER BY start_date DESC
    if "ix_leave_approval_date" not in existing:
        op.create_index(
            "ix_leave_approval_date",
            "leave_records",
            ["is_approved", "start_date"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = _existing_indexes(bind, "leave_records")

    if "ix_leave_approval_date" in existing:
        op.drop_index("ix_leave_approval_date", table_name="leave_records")
