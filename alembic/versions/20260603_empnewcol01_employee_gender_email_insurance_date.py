"""employee add gender / email / insurance_effective_date

Revision ID: empnewcol01
Revises: cmplfk01
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa

revision = "empnewcol01"
down_revision = "cmplfk01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("employees", sa.Column("gender", sa.String(length=10), nullable=True))
    op.add_column("employees", sa.Column("email", sa.String(length=100), nullable=True))
    op.add_column(
        "employees",
        sa.Column("insurance_effective_date", sa.Date(), nullable=True),
    )


def downgrade():
    op.drop_column("employees", "insurance_effective_date")
    op.drop_column("employees", "email")
    op.drop_column("employees", "gender")
