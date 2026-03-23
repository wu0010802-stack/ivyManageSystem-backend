"""add line_user_id to users and channel_secret to line_configs

為 users 表加入 LINE 個人綁定欄位，為 line_configs 加入 Webhook 驗證用 channel_secret。

Revision ID: i5j6k7l8m9n0
Revises: h4i5j6k7l8m9
Create Date: 2026-03-18 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "n1o2p3q4r5s6"
down_revision = "m9n0o1p2q3r4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # users.line_user_id
    users_cols = [c["name"] for c in inspector.get_columns("users")]
    if "line_user_id" not in users_cols:
        op.add_column(
            "users",
            sa.Column(
                "line_user_id",
                sa.String(100),
                nullable=True,
                comment="綁定的 LINE User ID",
            ),
        )
        op.create_index("ix_users_line_user_id", "users", ["line_user_id"], unique=True)

    # line_configs.channel_secret
    lc_cols = [c["name"] for c in inspector.get_columns("line_configs")]
    if "channel_secret" not in lc_cols:
        op.add_column(
            "line_configs",
            sa.Column(
                "channel_secret",
                sa.String(256),
                nullable=True,
                comment="LINE Channel Secret（Webhook 簽名驗證用）",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    indexes = {idx["name"] for idx in inspector.get_indexes("users")}
    if "ix_users_line_user_id" in indexes:
        op.drop_index("ix_users_line_user_id", table_name="users")

    users_cols = [c["name"] for c in inspector.get_columns("users")]
    if "line_user_id" in users_cols:
        op.drop_column("users", "line_user_id")

    lc_cols = [c["name"] for c in inspector.get_columns("line_configs")]
    if "channel_secret" in lc_cols:
        op.drop_column("line_configs", "channel_secret")
