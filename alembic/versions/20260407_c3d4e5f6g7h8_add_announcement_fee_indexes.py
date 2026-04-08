"""add announcement created_by and fee records student_period indexes

補齊缺少的索引：
- announcements: (created_by) — 加速查詢特定員工發佈的公告
- student_fee_records: (student_id, period) — 加速查詢單一學生特定學期費用

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-04-07 00:00:00.000000
"""

from alembic import op
from sqlalchemy import inspect

revision = "c3d4e5f6g7h8"
down_revision = "b2c3d4e5f6g7"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if "announcements" in tables:
        existing = _existing_indexes(bind, "announcements")
        if "ix_announcements_created_by" not in existing:
            op.create_index("ix_announcements_created_by", "announcements", ["created_by"])

    if "student_fee_records" in tables:
        existing = _existing_indexes(bind, "student_fee_records")
        if "ix_fee_records_student_period" not in existing:
            op.create_index(
                "ix_fee_records_student_period",
                "student_fee_records",
                ["student_id", "period"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if "student_fee_records" in tables:
        existing = _existing_indexes(bind, "student_fee_records")
        if "ix_fee_records_student_period" in existing:
            op.drop_index("ix_fee_records_student_period", table_name="student_fee_records")

    if "announcements" in tables:
        existing = _existing_indexes(bind, "announcements")
        if "ix_announcements_created_by" in existing:
            op.drop_index("ix_announcements_created_by", table_name="announcements")
