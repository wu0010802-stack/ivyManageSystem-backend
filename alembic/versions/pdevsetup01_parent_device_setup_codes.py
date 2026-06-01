"""parent_device_setup_codes：無 LINE 家長裝置登入設定碼

Revision ID: pdevsetup01
Revises: eb0d4cf88f26
Create Date: 2026-05-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "pdevsetup01"
down_revision: Union[str, Sequence[str], None] = "eb0d4cf88f26"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "parent_device_setup_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "guardian_id",
            sa.Integer(),
            sa.ForeignKey("guardians.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column(
            "used_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_parent_device_setup_codes_guardian_id",
        "parent_device_setup_codes",
        ["guardian_id"],
    )
    op.create_index(
        "ix_parent_device_setup_expires_unused",
        "parent_device_setup_codes",
        ["expires_at", "used_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_parent_device_setup_expires_unused",
        table_name="parent_device_setup_codes",
    )
    op.drop_index(
        "ix_parent_device_setup_codes_guardian_id",
        table_name="parent_device_setup_codes",
    )
    op.drop_table("parent_device_setup_codes")
