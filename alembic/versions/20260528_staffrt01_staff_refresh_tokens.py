"""staff_refresh_tokens table for Spec F rotation

Revision ID: staffrt01
Revises: intghealth01
Create Date: 2026-05-28
"""

import sqlalchemy as sa
from alembic import op

revision = "staffrt01"
down_revision = "intghealth01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "staff_refresh_tokens",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.BigInteger,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("family_id", sa.String(36), nullable=False),
        sa.Column(
            "token_hash",
            sa.String(64),
            nullable=False,
            unique=True,
            comment="sha256(raw refresh token) hex；DB 不存明文",
        ),
        sa.Column(
            "parent_token_id",
            sa.BigInteger,
            sa.ForeignKey("staff_refresh_tokens.id", ondelete="SET NULL"),
            nullable=True,
            comment="rotation 上一個 token；可追溯 family",
        ),
        sa.Column(
            "used_at",
            sa.DateTime,
            nullable=True,
            comment="rotation 後填入；reuse 偵測欄位",
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime,
            nullable=True,
            comment="family 全撤銷時填入",
        ),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column(
            "user_agent",
            sa.String(255),
            nullable=True,
            comment="觀測用，不參與決策",
        ),
        sa.Column(
            "ip",
            sa.String(45),
            nullable=True,
            comment="IPv6 預留；觀測用",
        ),
    )
    op.create_index(
        "ix_staff_refresh_user_family",
        "staff_refresh_tokens",
        ["user_id", "family_id"],
    )
    op.create_index(
        "ix_staff_refresh_expires_at",
        "staff_refresh_tokens",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_staff_refresh_expires_at", table_name="staff_refresh_tokens")
    op.drop_index("ix_staff_refresh_user_family", table_name="staff_refresh_tokens")
    op.drop_table("staff_refresh_tokens")
