"""student growth reports table (P3 of growth profile)

Adds student_growth_reports table for tracking generated PDF reports.

Revision ID: g6h7i8j9k0l1
Revises: f5a6b7c8d9e0
Create Date: 2026-05-15
"""

from alembic import op
import sqlalchemy as sa

revision = "g6h7i8j9k0l1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_growth_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "student_id",
            sa.Integer(),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_label", sa.String(40), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("file_path", sa.String(255), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "generated_by",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("line_sent_at", sa.DateTime(), nullable=True),
        sa.Column("parent_first_viewed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "parent_view_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("teacher_narrative", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "period_start <= period_end",
            name="ck_growth_report_period_order",
        ),
    )
    op.create_index(
        "ix_growth_reports_student_period",
        "student_growth_reports",
        ["student_id", "period_start", "period_end"],
    )
    op.create_index(
        "ix_growth_reports_status",
        "student_growth_reports",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_growth_reports_status", table_name="student_growth_reports")
    op.drop_index(
        "ix_growth_reports_student_period", table_name="student_growth_reports"
    )
    op.drop_table("student_growth_reports")
