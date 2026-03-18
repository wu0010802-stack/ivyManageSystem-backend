"""add classroom term fields

Revision ID: a1b2c3d4e5f6
Revises: 8f1b2c3d4e5f
Create Date: 2026-03-13 16:20:00.000000
"""

from __future__ import annotations

from datetime import date

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "8f1b2c3d4e5f"
branch_labels = None
depends_on = None


def _current_academic_term() -> tuple[int, int]:
    today = date.today()
    if today.month >= 8:
        return today.year, 1
    if today.month >= 2:
        return today.year - 1, 2
    return today.year - 1, 1


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    school_year, semester = _current_academic_term()

    with op.batch_alter_table("classrooms") as batch_op:
        batch_op.add_column(sa.Column("school_year", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("semester", sa.Integer(), nullable=True))

    bind.execute(
        sa.text(
            """
            UPDATE classrooms
            SET school_year = :school_year,
                semester = :semester
            WHERE school_year IS NULL OR semester IS NULL
            """
        ),
        {"school_year": school_year, "semester": semester},
    )

    unique_constraints = inspector.get_unique_constraints("classrooms")
    for constraint in unique_constraints:
        column_names = constraint.get("column_names") or []
        if column_names == ["name"] and constraint.get("name"):
            with op.batch_alter_table("classrooms") as batch_op:
                batch_op.drop_constraint(constraint["name"], type_="unique")

    with op.batch_alter_table("classrooms") as batch_op:
        batch_op.alter_column("school_year", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("semester", existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint(
            "uq_classrooms_term_name",
            ["school_year", "semester", "name"],
        )
        batch_op.create_index(
            "ix_classrooms_term_active",
            ["school_year", "semester", "is_active"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("classrooms") as batch_op:
        batch_op.drop_index("ix_classrooms_term_active")
        batch_op.drop_constraint("uq_classrooms_term_name", type_="unique")
        batch_op.create_unique_constraint("uq_classrooms_name", ["name"])
        batch_op.drop_column("semester")
        batch_op.drop_column("school_year")
