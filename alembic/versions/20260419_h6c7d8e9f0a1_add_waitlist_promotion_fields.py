"""add waitlist promotion fields to registration_courses

候補轉正智能化：新增 promoted_at / confirm_deadline / reminder_sent_at
支援 status='promoted_pending'（升正式待家長於期限內確認），逾期由
背景排程自動放棄並遞補下一位。

Revision ID: h6c7d8e9f0a1
Revises: g5b6c7d8e9f0
Create Date: 2026-04-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "h6c7d8e9f0a1"
down_revision = "g5b6c7d8e9f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "registration_courses" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("registration_courses")}
    if "promoted_at" not in existing_cols:
        op.add_column(
            "registration_courses",
            sa.Column("promoted_at", sa.DateTime, nullable=True),
        )
    if "confirm_deadline" not in existing_cols:
        op.add_column(
            "registration_courses",
            sa.Column("confirm_deadline", sa.DateTime, nullable=True),
        )
    if "reminder_sent_at" not in existing_cols:
        op.add_column(
            "registration_courses",
            sa.Column("reminder_sent_at", sa.DateTime, nullable=True),
        )

    existing_idx = {ix["name"] for ix in inspector.get_indexes("registration_courses")}
    if "ix_reg_courses_pending_deadline" not in existing_idx:
        op.create_index(
            "ix_reg_courses_pending_deadline",
            "registration_courses",
            ["status", "confirm_deadline"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "registration_courses" not in inspector.get_table_names():
        return

    existing_idx = {ix["name"] for ix in inspector.get_indexes("registration_courses")}
    if "ix_reg_courses_pending_deadline" in existing_idx:
        op.drop_index(
            "ix_reg_courses_pending_deadline", table_name="registration_courses"
        )

    existing_cols = {c["name"] for c in inspector.get_columns("registration_courses")}
    for col in ("reminder_sent_at", "confirm_deadline", "promoted_at"):
        if col in existing_cols:
            op.drop_column("registration_courses", col)
