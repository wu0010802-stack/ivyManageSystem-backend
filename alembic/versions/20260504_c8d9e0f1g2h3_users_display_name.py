"""add display_name to users

家長端 hero 顯示用：home_summary / profile / liff_login 等端點
原本回傳 user.username（內部 parent_line_<id>），改回 display_name。
LIFF 登入時以 LINE id_token payload['name'] 寫入；行政建檔的 Guardian.name
仍保留作為 fallback（在應用層 helper 處理）。

Revision ID: c8d9e0f1g2h3
Revises: b7c8d9e0f1g2
Create Date: 2026-05-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "c8d9e0f1g2h3"
down_revision = "b7c8d9e0f1g2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "users" not in inspector.get_table_names():
        return
    users_cols = [c["name"] for c in inspector.get_columns("users")]
    if "display_name" not in users_cols:
        op.add_column(
            "users",
            sa.Column(
                "display_name",
                sa.String(100),
                nullable=True,
                comment="顯示名（家長端 hero / 問候語）；LIFF 登入時取 LINE displayName 寫入",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "users" not in inspector.get_table_names():
        return
    users_cols = [c["name"] for c in inspector.get_columns("users")]
    if "display_name" in users_cols:
        op.drop_column("users", "display_name")
