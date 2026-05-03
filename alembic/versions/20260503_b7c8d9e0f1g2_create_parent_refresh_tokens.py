"""create parent_refresh_tokens table

家長端 30 天免重登：rotation + reuse detection。
詳見 spec：docs/superpowers/specs/2026-05-03-parent-line-refresh-token-design.md

Revision ID: b7c8d9e0f1g2
Revises: r3s4t5u6v7w8
Create Date: 2026-05-03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "b7c8d9e0f1g2"
down_revision = "r3s4t5u6v7w8"
branch_labels = None
depends_on = None


def _index_names(bind, table: str) -> set:
    if table not in inspect(bind).get_table_names():
        return set()
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "parent_refresh_tokens" in tables:
        return
    if "users" not in tables:
        return

    op.create_table(
        "parent_refresh_tokens",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("family_id", sa.String(length=36), nullable=False),
        sa.Column(
            "token_hash",
            sa.String(length=64),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "parent_token_id",
            sa.BigInteger,
            sa.ForeignKey("parent_refresh_tokens.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("used_at", sa.DateTime, nullable=True),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
    )
    op.create_index(
        "ix_parent_refresh_user_family",
        "parent_refresh_tokens",
        ["user_id", "family_id"],
    )
    op.create_index(
        "ix_parent_refresh_expires_at",
        "parent_refresh_tokens",
        ["expires_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "parent_refresh_tokens" not in inspect(bind).get_table_names():
        return
    existing = _index_names(bind, "parent_refresh_tokens")
    if "ix_parent_refresh_expires_at" in existing:
        op.drop_index(
            "ix_parent_refresh_expires_at", table_name="parent_refresh_tokens"
        )
    if "ix_parent_refresh_user_family" in existing:
        op.drop_index(
            "ix_parent_refresh_user_family", table_name="parent_refresh_tokens"
        )
    op.drop_table("parent_refresh_tokens")
