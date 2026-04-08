"""add activity attendance tables

Revision ID: z5a6b7c8d9e0
Revises: y4z5a6b7c8d9
Create Date: 2026-04-01
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "z5a6b7c8d9e0"
down_revision = "y4z5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = inspector.get_table_names()

    if "activity_sessions" not in existing_tables:
        op.create_table(
            "activity_sessions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("course_id", sa.Integer(), nullable=False),
            sa.Column("session_date", sa.Date(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["course_id"], ["activity_courses.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("course_id", "session_date", name="uq_activity_session_course_date"),
        )
        op.create_index("ix_activity_sessions_course_id", "activity_sessions", ["course_id"])
        op.create_index("ix_activity_sessions_date", "activity_sessions", ["session_date"])

    if "activity_attendances" not in existing_tables:
        op.create_table(
            "activity_attendances",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("session_id", sa.Integer(), nullable=False),
            sa.Column("registration_id", sa.Integer(), nullable=False),
            sa.Column("is_present", sa.Boolean(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("recorded_by", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["session_id"], ["activity_sessions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["registration_id"], ["activity_registrations.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("session_id", "registration_id", name="uq_activity_attendance_session_reg"),
        )
        op.create_index("ix_activity_attendances_session_id", "activity_attendances", ["session_id"])
        op.create_index("ix_activity_attendances_reg_id", "activity_attendances", ["registration_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = inspector.get_table_names()

    if "activity_attendances" in existing_tables:
        op.drop_index("ix_activity_attendances_reg_id", table_name="activity_attendances")
        op.drop_index("ix_activity_attendances_session_id", table_name="activity_attendances")
        op.drop_table("activity_attendances")

    if "activity_sessions" in existing_tables:
        op.drop_index("ix_activity_sessions_date", table_name="activity_sessions")
        op.drop_index("ix_activity_sessions_course_id", table_name="activity_sessions")
        op.drop_table("activity_sessions")
