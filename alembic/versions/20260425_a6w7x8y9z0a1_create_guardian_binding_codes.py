"""create guardian_binding_codes table

家長入口 Batch 1（地基）：
- 建立 guardian_binding_codes 表
- 欄位設計詳見 models/parent_binding.py
- claim 邏輯使用 atomic UPDATE WHERE used_at IS NULL，欄位上不需 unique 多重約束

Revision ID: a6w7x8y9z0a1
Revises: z5v6w7x8y9z0
Create Date: 2026-04-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "a6w7x8y9z0a1"
down_revision = "z5v6w7x8y9z0"
branch_labels = None
depends_on = None


def _index_names(bind, table: str) -> set:
    if table not in inspect(bind).get_table_names():
        return set()
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "guardian_binding_codes" in tables:
        return
    if "guardians" not in tables or "users" not in tables:
        return

    op.create_table(
        "guardian_binding_codes",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "guardian_id",
            sa.Integer,
            sa.ForeignKey("guardians.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "code_hash",
            sa.String(length=64),
            nullable=False,
            unique=True,
        ),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("used_at", sa.DateTime, nullable=True),
        sa.Column(
            "used_by_user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            sa.Integer,
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_guardian_binding_expires_unused",
        "guardian_binding_codes",
        ["expires_at", "used_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "guardian_binding_codes" not in inspect(bind).get_table_names():
        return
    if "ix_guardian_binding_expires_unused" in _index_names(bind, "guardian_binding_codes"):
        op.drop_index(
            "ix_guardian_binding_expires_unused",
            table_name="guardian_binding_codes",
        )
    op.drop_table("guardian_binding_codes")
