"""employee.pii_redacted_at column

Revision ID: emppiired01
Revises: intghealth01
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa

revision = "emppiired01"
down_revision = "intghealth01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("pii_redacted_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("employees", "pii_redacted_at")
