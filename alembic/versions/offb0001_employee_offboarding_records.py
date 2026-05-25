"""employee_offboarding_records + SalaryRecord.unused_leave_payout

Revision ID: offb0001
Revises: mergeheads02
Create Date: 2026-05-25

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "offb0001"
down_revision: Union[str, Sequence[str], None] = "mergeheads02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employee_offboarding_records",
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("resign_date", sa.Date(), nullable=False),
        sa.Column("resign_reason", sa.Text(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("opened_by_user_id", sa.Integer(), nullable=False),
        sa.Column("user_revoked_at", sa.DateTime(), nullable=True),
        sa.Column("appraisal_marked_at", sa.DateTime(), nullable=True),
        sa.Column("leave_snapshot_at", sa.DateTime(), nullable=True),
        sa.Column("certificate_generated_at", sa.DateTime(), nullable=True),
        sa.Column(
            "leave_balance_snapshot",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
        sa.Column("certificate_pdf_path", sa.Text(), nullable=True),
        sa.Column("nhi_unenroll_submitted_at", sa.DateTime(), nullable=True),
        sa.Column("magic_link_token_hash", sa.Text(), nullable=True),
        sa.Column("magic_link_expires_at", sa.DateTime(), nullable=True),
        sa.Column("magic_link_revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "magic_link_download_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("magic_link_last_used_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("closed_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opened_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["closed_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("employee_id"),
    )
    op.create_index(
        "ix_offboarding_resign_date",
        "employee_offboarding_records",
        ["resign_date"],
    )

    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.create_index(
            "ix_offboarding_open_status",
            "employee_offboarding_records",
            ["closed_at"],
            postgresql_where=sa.text("closed_at IS NULL"),
        )
    else:
        op.create_index(
            "ix_offboarding_open_status",
            "employee_offboarding_records",
            ["closed_at"],
            sqlite_where=sa.text("closed_at IS NULL"),
        )

    op.add_column(
        "salary_records",
        sa.Column(
            "unused_leave_payout",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
            comment="特休未休折現（§38；獨立 column 不進 gross_salary）",
        ),
    )


def downgrade() -> None:
    op.drop_column("salary_records", "unused_leave_payout")
    op.drop_index("ix_offboarding_open_status", "employee_offboarding_records")
    op.drop_index("ix_offboarding_resign_date", "employee_offboarding_records")
    op.drop_table("employee_offboarding_records")
