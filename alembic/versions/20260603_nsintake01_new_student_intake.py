"""new student intake: provisional seat fields + grade_intake_targets

Revision ID: nsintake01
Revises: yeatpunch01
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa

revision = "nsintake01"
down_revision = "yeatpunch01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "recruitment_visits",
        sa.Column("provisional_grade_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "recruitment_visits",
        sa.Column("target_school_year", sa.Integer(), nullable=True),
    )
    op.add_column(
        "recruitment_visits",
        sa.Column("target_semester", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_rv_provisional_grade",
        "recruitment_visits",
        "class_grades",
        ["provisional_grade_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_rv_target_grade",
        "recruitment_visits",
        ["target_school_year", "target_semester", "provisional_grade_id"],
    )

    op.create_table(
        "grade_intake_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "grade_id",
            sa.Integer(),
            sa.ForeignKey("class_grades.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("school_year", sa.Integer(), nullable=False),
        sa.Column("semester", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("target_seats", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "uq_grade_intake_target",
        "grade_intake_targets",
        ["grade_id", "school_year", "semester"],
        unique=True,
    )


def downgrade():
    op.drop_index("uq_grade_intake_target", table_name="grade_intake_targets")
    op.drop_table("grade_intake_targets")
    op.drop_index("ix_rv_target_grade", table_name="recruitment_visits")
    op.drop_constraint(
        "fk_rv_provisional_grade", "recruitment_visits", type_="foreignkey"
    )
    op.drop_column("recruitment_visits", "target_semester")
    op.drop_column("recruitment_visits", "target_school_year")
    op.drop_column("recruitment_visits", "provisional_grade_id")
