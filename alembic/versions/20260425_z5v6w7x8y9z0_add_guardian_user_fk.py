"""add Guardian.user_id FK + User.line_follow_confirmed_at

家長入口 Batch 1（地基）：
- guardians 加 user_id FK 指向 users（ON DELETE SET NULL），允許多筆 Guardian 共用同一 User
- users 加 line_follow_confirmed_at（推播可達性旗標：bot follow webhook 寫入時間）

Revision ID: z5v6w7x8y9z0
Revises: y4u5v6w7x8y9
Create Date: 2026-04-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "z5v6w7x8y9z0"
down_revision = "y4u5v6w7x8y9"
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
    tables = inspect(bind).get_table_names()

    # --- guardians.user_id ---
    if "guardians" in tables:
        cols = _column_names(bind, "guardians")
        if "user_id" not in cols:
            op.add_column(
                "guardians",
                sa.Column("user_id", sa.Integer, nullable=True),
            )
            # FK 與 index 分步建立以便冪等
            with op.batch_alter_table("guardians") as batch:
                batch.create_foreign_key(
                    "fk_guardians_user_id",
                    "users",
                    ["user_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
        if "ix_guardians_user" not in _index_names(bind, "guardians"):
            op.create_index("ix_guardians_user", "guardians", ["user_id"])

    # --- users.line_follow_confirmed_at ---
    if "users" in tables:
        cols = _column_names(bind, "users")
        if "line_follow_confirmed_at" not in cols:
            op.add_column(
                "users",
                sa.Column("line_follow_confirmed_at", sa.DateTime, nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()

    if "guardians" in tables:
        if "ix_guardians_user" in _index_names(bind, "guardians"):
            op.drop_index("ix_guardians_user", table_name="guardians")
        if "user_id" in _column_names(bind, "guardians"):
            with op.batch_alter_table("guardians") as batch:
                try:
                    batch.drop_constraint("fk_guardians_user_id", type_="foreignkey")
                except Exception:
                    pass
                batch.drop_column("user_id")

    if "users" in tables and "line_follow_confirmed_at" in _column_names(bind, "users"):
        op.drop_column("users", "line_follow_confirmed_at")
