"""add final_reminder_sent_at to registration_courses

T-6h 最後提醒戳記，與既有 reminder_sent_at（T-24h）區隔。

Revision ID: 17fa49f72231
Revises: g6h7i8j9k0l1
Create Date: 2026-05-13 13:17:59.597004

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "17fa49f72231"
down_revision = "g6h7i8j9k0l1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "registration_courses" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("registration_courses")}
    if "final_reminder_sent_at" not in existing_cols:
        op.add_column(
            "registration_courses",
            sa.Column("final_reminder_sent_at", sa.DateTime, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "registration_courses" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("registration_courses")}
    if "final_reminder_sent_at" in existing_cols:
        op.drop_column("registration_courses", "final_reminder_sent_at")
