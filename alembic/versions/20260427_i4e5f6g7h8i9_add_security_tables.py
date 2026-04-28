"""新增 rate_limit_buckets 與 jwt_blocklist 兩張表

對應安全收口 spec（2026-04-27）：
- LOW-1：rate limiter 從 in-process dict 改為 PG-backed
- LOW-2：JWT jti 黑名單，支援 logout 立即廢止 token

Revision ID: i4e5f6g7h8i9
Revises: h3d4e5f6g7h8
Create Date: 2026-04-27
"""

import sqlalchemy as sa
from alembic import op

revision = "i4e5f6g7h8i9"
down_revision = "h3d4e5f6g7h8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "rate_limit_buckets",
        sa.Column("bucket_key", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("bucket_key", "window_start"),
    )
    op.create_index(
        "ix_rate_limit_buckets_window_start",
        "rate_limit_buckets",
        ["window_start"],
    )

    op.create_table(
        "jwt_blocklist",
        sa.Column("jti", sa.Text(), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_jwt_blocklist_expires_at",
        "jwt_blocklist",
        ["expires_at"],
    )


def downgrade():
    op.drop_index("ix_jwt_blocklist_expires_at", table_name="jwt_blocklist")
    op.drop_table("jwt_blocklist")
    op.drop_index("ix_rate_limit_buckets_window_start", table_name="rate_limit_buckets")
    op.drop_table("rate_limit_buckets")
