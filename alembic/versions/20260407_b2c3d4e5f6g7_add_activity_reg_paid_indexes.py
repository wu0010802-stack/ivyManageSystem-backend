"""add activity registration is_paid indexes

新增課後才藝報名 is_paid 相關索引，加速繳費狀態篩選查詢。
- activity_registrations: (is_paid) — 繳費狀態單欄篩選
- activity_registrations: (is_active, is_paid) — 同時過濾有效報名與繳費狀態

Revision ID: b2c3d4e5f6g7
Revises: a2b3c4d5e6f7
Create Date: 2026-04-07 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "b2c3d4e5f6g7"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if "activity_registrations" not in _existing_tables(bind):
        return
    existing = _existing_indexes(bind, "activity_registrations")

    if "ix_activity_regs_paid" not in existing:
        op.create_index(
            "ix_activity_regs_paid",
            "activity_registrations",
            ["is_paid"],
        )
    if "ix_activity_regs_active_paid" not in existing:
        op.create_index(
            "ix_activity_regs_active_paid",
            "activity_registrations",
            ["is_active", "is_paid"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "activity_registrations" not in _existing_tables(bind):
        return
    existing = _existing_indexes(bind, "activity_registrations")

    if "ix_activity_regs_active_paid" in existing:
        op.drop_index("ix_activity_regs_active_paid", table_name="activity_registrations")
    if "ix_activity_regs_paid" in existing:
        op.drop_index("ix_activity_regs_paid", table_name="activity_registrations")
