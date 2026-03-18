"""add student_dismissal_calls table

Revision ID: d5e6f7a8b9c0
Revises: a1b2c3d4e5f6
Create Date: 2026-03-16 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = "d5e6f7a8b9c0"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "student_dismissal_calls" in inspector.get_table_names():
        return

    op.create_table(
        "student_dismissal_calls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("classroom_id", sa.Integer(), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("acknowledged_by_employee_id", sa.Integer(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("completed_by_employee_id", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"]),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"]),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["acknowledged_by_employee_id"], ["employees.id"]),
        sa.ForeignKeyConstraint(["completed_by_employee_id"], ["employees.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dismissal_calls_student_active",
        "student_dismissal_calls", ["student_id", "status"],
    )
    op.create_index(
        "ix_dismissal_calls_classroom_status",
        "student_dismissal_calls", ["classroom_id", "status"],
    )
    op.create_index(
        "ix_dismissal_calls_requested_at",
        "student_dismissal_calls", ["requested_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "student_dismissal_calls" not in inspector.get_table_names():
        return

    op.drop_index("ix_dismissal_calls_requested_at", table_name="student_dismissal_calls")
    op.drop_index("ix_dismissal_calls_classroom_status", table_name="student_dismissal_calls")
    op.drop_index("ix_dismissal_calls_student_active", table_name="student_dismissal_calls")
    op.drop_table("student_dismissal_calls")
