"""add appraisal_year_end_bonus to salary_records

Revision ID: ayebsr1
Revises: rfunnel01
Create Date: 2026-05-22

考核年終獎金 column，獨立於 gross_salary，每月 calculate 時刷新（2 月才有值）。
"""

from alembic import op
import sqlalchemy as sa

revision = "ayebsr1"
down_revision = "rfunnel01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "salary_records",
        sa.Column(
            "appraisal_year_end_bonus",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
            comment="考核年終獎金（2/5 與月薪同發；自 special_bonus_items 兩筆 APPRAISAL_HALF_BONUS_* SUM；不進 gross_salary）",
        ),
    )


def downgrade() -> None:
    op.drop_column("salary_records", "appraisal_year_end_bonus")
