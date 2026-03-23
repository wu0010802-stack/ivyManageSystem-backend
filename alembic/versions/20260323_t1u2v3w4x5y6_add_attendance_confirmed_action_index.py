"""add attendance confirmed_action index

補齊考勤異常確認查詢索引：
- attendances: (confirmed_action, attendance_date) — 篩出未確認異常的高頻查詢

Revision ID: t1u2v3w4x5y6
Revises: r5s6t7u8v9w0
Create Date: 2026-03-23 00:00:00.000000
"""

from alembic import op
from sqlalchemy import inspect

revision = "t1u2v3w4x5y6"
down_revision = "r5s6t7u8v9w0"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    # attendances: (confirmed_action, attendance_date)
    # 異常確認頁每日查詢 confirmed_action IS NULL/IS NOT NULL，無索引支援
    if "attendances" in tables:
        existing = _existing_indexes(bind, "attendances")
        if "ix_attendance_confirmed_action" not in existing:
            op.create_index(
                "ix_attendance_confirmed_action",
                "attendances",
                ["confirmed_action", "attendance_date"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if "attendances" in tables:
        existing = _existing_indexes(bind, "attendances")
        if "ix_attendance_confirmed_action" in existing:
            op.drop_index("ix_attendance_confirmed_action", table_name="attendances")
