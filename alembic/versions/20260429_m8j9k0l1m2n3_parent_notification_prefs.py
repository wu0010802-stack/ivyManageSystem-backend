"""家長入口 2.0 — Phase 6：parent_notification_preferences

Revision ID: m8j9k0l1m2n3
Revises: l7i8j9k0l1m2
Create Date: 2026-04-29
"""

import sqlalchemy as sa
from alembic import op

revision = "m8j9k0l1m2n3"
down_revision = "l7i8j9k0l1m2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "parent_notification_preferences",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column(
            "channel",
            sa.String(length=10),
            nullable=False,
            server_default="line",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "event_type",
            "channel",
            name="uq_parent_notif_pref_triple",
        ),
    )
    op.create_index(
        "ix_parent_notif_pref_user",
        "parent_notification_preferences",
        ["user_id"],
    )


def downgrade():
    op.drop_index(
        "ix_parent_notif_pref_user", table_name="parent_notification_preferences"
    )
    op.drop_table("parent_notification_preferences")
