"""student measurements and milestones (P1 of growth profile)

Adds two new tables to the portfolio domain:

1. student_measurements: 身高/體重/視力等量測（無 soft delete；純資料）
2. student_milestones: 結構化里程碑（含 soft delete；家長可 acknowledge / react）

Revision ID: d3e4f5a6b7c8
Revises: e4f5a6b7c8d9
Create Date: 2026-05-13

"""

from alembic import op
import sqlalchemy as sa

revision = "d3e4f5a6b7c8"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_measurements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "student_id",
            sa.Integer(),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("measured_on", sa.Date(), nullable=False),
        sa.Column("height_cm", sa.Numeric(5, 2), nullable=True),
        sa.Column("weight_kg", sa.Numeric(5, 2), nullable=True),
        sa.Column("head_circumference_cm", sa.Numeric(5, 2), nullable=True),
        sa.Column("vision_left", sa.Numeric(3, 2), nullable=True),
        sa.Column("vision_right", sa.Numeric(3, 2), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "height_cm IS NOT NULL OR weight_kg IS NOT NULL "
            "OR head_circumference_cm IS NOT NULL "
            "OR vision_left IS NOT NULL OR vision_right IS NOT NULL",
            name="ck_measurement_at_least_one_value",
        ),
    )
    op.create_index(
        "ix_student_measurements_student_date",
        "student_measurements",
        ["student_id", "measured_on"],
    )

    op.create_table(
        "student_milestones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "student_id",
            sa.Integer(),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("milestone_type", sa.String(40), nullable=False),
        sa.Column("achieved_on", sa.Date(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon", sa.String(40), nullable=True),
        sa.Column(
            "source_type",
            sa.String(30),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column("source_ref_type", sa.String(30), nullable=True),
        sa.Column("source_ref_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("parent_acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column(
            "parent_acknowledged_by",
            sa.Integer(),
            sa.ForeignKey("guardians.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("parent_reaction", sa.String(10), nullable=True),
    )
    op.create_index(
        "ix_student_milestones_student_date",
        "student_milestones",
        ["student_id", "achieved_on"],
    )
    # 自動觸發的 milestone 需 dedup (同學生 + 同類型 + 同日 + 同源 ref)
    op.create_index(
        "uq_milestone_dedup",
        "student_milestones",
        [
            "student_id",
            "milestone_type",
            "achieved_on",
            "source_type",
            "source_ref_type",
            "source_ref_id",
        ],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_milestone_dedup", table_name="student_milestones")
    op.drop_index("ix_student_milestones_student_date", table_name="student_milestones")
    op.drop_table("student_milestones")
    op.drop_index(
        "ix_student_measurements_student_date", table_name="student_measurements"
    )
    op.drop_table("student_measurements")
