"""add config_year to position_salary_configs and attendance_policies

Revision ID: cfgyear01
Revises: yebnd01
Create Date: 2026-06-05
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "cfgyear01"
down_revision: Union[str, Sequence[str], None] = "yebnd01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CURRENT_YEAR = 2026


def upgrade() -> None:
    op.add_column(
        "position_salary_configs",
        sa.Column("config_year", sa.Integer(), nullable=True),
    )
    op.execute(
        f"UPDATE position_salary_configs SET config_year = {_CURRENT_YEAR} "
        "WHERE config_year IS NULL"
    )
    op.alter_column("position_salary_configs", "config_year", nullable=False)
    op.create_index(
        "ix_position_salary_config_year",
        "position_salary_configs",
        ["config_year", "version"],
    )

    op.add_column(
        "attendance_policies",
        sa.Column("config_year", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE attendance_policies "
        "SET config_year = EXTRACT(YEAR FROM effective_date)::int "
        "WHERE effective_date IS NOT NULL AND config_year IS NULL"
    )
    op.execute(
        f"UPDATE attendance_policies SET config_year = {_CURRENT_YEAR} "
        "WHERE config_year IS NULL"
    )
    op.alter_column("attendance_policies", "config_year", nullable=False)
    op.create_index(
        "ix_attendance_policy_config_year",
        "attendance_policies",
        ["config_year", "version"],
    )


def downgrade() -> None:
    op.drop_index("ix_attendance_policy_config_year", table_name="attendance_policies")
    op.drop_column("attendance_policies", "config_year")
    op.drop_index(
        "ix_position_salary_config_year", table_name="position_salary_configs"
    )
    op.drop_column("position_salary_configs", "config_year")
