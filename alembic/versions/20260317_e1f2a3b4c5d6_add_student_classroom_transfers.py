"""add student_classroom_transfers table

Revision ID: e1f2a3b4c5d6
Revises: d5e6f7a8b9c0
Create Date: 2026-03-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "e1f2a3b4c5d6"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "student_classroom_transfers" in inspector.get_table_names():
        return

    op.create_table(
        "student_classroom_transfers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("from_classroom_id", sa.Integer(), nullable=True),
        sa.Column("to_classroom_id", sa.Integer(), nullable=False),
        sa.Column("transferred_at", sa.DateTime(), nullable=False),
        sa.Column("transferred_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"]),
        sa.ForeignKeyConstraint(["from_classroom_id"], ["classrooms.id"]),
        sa.ForeignKeyConstraint(["to_classroom_id"], ["classrooms.id"]),
        sa.ForeignKeyConstraint(["transferred_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_student_transfers_student", "student_classroom_transfers", ["student_id"])
    op.create_index("ix_student_transfers_at", "student_classroom_transfers", ["transferred_at"])
    op.create_index(
        "ix_student_transfers_to_classroom",
        "student_classroom_transfers", ["to_classroom_id", "transferred_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "student_classroom_transfers" not in inspector.get_table_names():
        return

    op.drop_index("ix_student_transfers_to_classroom", table_name="student_classroom_transfers")
    op.drop_index("ix_student_transfers_at", table_name="student_classroom_transfers")
    op.drop_index("ix_student_transfers_student", table_name="student_classroom_transfers")
    op.drop_table("student_classroom_transfers")
