"""add supervisor_role to employees

Revision ID: c6a7b9d1e2f3
Revises: 4ddf3ebad3e8
Create Date: 2026-03-12 15:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c6a7b9d1e2f3"
down_revision = "4ddf3ebad3e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("supervisor_role", sa.String(length=20), nullable=True, comment="主管職 (園長/主任/組長/副組長)"),
    )


def downgrade() -> None:
    op.drop_column("employees", "supervisor_role")
