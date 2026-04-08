"""add activity attendance session_present composite index

新增 (session_id, is_present) 複合索引，加速 GROUP BY 聚合查詢。

Revision ID: a1b2c3d4e5f6
Revises: z5a6b7c8d9e0
Create Date: 2026-04-02 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "a1b2c3d4e5f6"
down_revision = "z5a6b7c8d9e0"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if "activity_attendances" in _existing_tables(bind):
        existing = _existing_indexes(bind, "activity_attendances")
        if "ix_activity_attendances_session_present" not in existing:
            op.create_index(
                "ix_activity_attendances_session_present",
                "activity_attendances",
                ["session_id", "is_present"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    if "activity_attendances" in _existing_tables(bind):
        existing = _existing_indexes(bind, "activity_attendances")
        if "ix_activity_attendances_session_present" in existing:
            op.drop_index(
                "ix_activity_attendances_session_present",
                table_name="activity_attendances",
            )
