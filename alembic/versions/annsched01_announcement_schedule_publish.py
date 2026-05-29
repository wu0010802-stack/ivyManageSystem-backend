"""announcements: add publish_at and expires_at columns

Revision ID: annsched01
Revises: eb0d4cf88f26
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa

revision = "annsched01"
down_revision = "eb0d4cf88f26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "announcements",
        sa.Column("publish_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "announcements",
        sa.Column("expires_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_announcements_publish_at",
        "announcements",
        ["publish_at"],
    )
    op.create_index(
        "ix_announcements_expires_at",
        "announcements",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_announcements_expires_at", table_name="announcements")
    op.drop_index("ix_announcements_publish_at", table_name="announcements")
    op.drop_column("announcements", "expires_at")
    op.drop_column("announcements", "publish_at")
