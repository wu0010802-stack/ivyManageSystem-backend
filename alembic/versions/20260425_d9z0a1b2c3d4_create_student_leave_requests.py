"""create student_leave_requests

家長入口 Batch 5：家長端學生請假申請 + 教師審核。

Revision ID: d9z0a1b2c3d4
Revises: c8y9z0a1b2c3
Create Date: 2026-04-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "d9z0a1b2c3d4"
down_revision = "c8y9z0a1b2c3"
branch_labels = None
depends_on = None


def _index_names(bind, table: str) -> set:
    if table not in inspect(bind).get_table_names():
        return set()
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "student_leave_requests" in tables:
        return
    if not {"students", "users", "guardians"}.issubset(tables):
        return

    op.create_table(
        "student_leave_requests",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "student_id",
            sa.Integer,
            sa.ForeignKey("students.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "applicant_user_id",
            sa.Integer,
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "applicant_guardian_id",
            sa.Integer,
            sa.ForeignKey("guardians.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("leave_type", sa.String(length=10), nullable=False),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("attachment_path", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=15),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "reviewed_by",
            sa.Integer,
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime, nullable=True),
        sa.Column("review_note", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_slr_student_daterange",
        "student_leave_requests",
        ["student_id", "start_date", "end_date"],
    )
    op.create_index(
        "ix_slr_status_created",
        "student_leave_requests",
        ["status", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "student_leave_requests" not in inspect(bind).get_table_names():
        return
    for ix in ("ix_slr_status_created", "ix_slr_student_daterange"):
        if ix in _index_names(bind, "student_leave_requests"):
            op.drop_index(ix, table_name="student_leave_requests")
    op.drop_table("student_leave_requests")
