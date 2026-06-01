"""audit_logs 加 impersonated_by / impersonated_by_name

Revision ID: portalimp01
Revises: eb0d4cf88f26
Create Date: 2026-06-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision = "portalimp01"
down_revision: Union[str, Sequence[str], None] = "eb0d4cf88f26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column("impersonated_by", sa.Integer(), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("impersonated_by_name", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_logs", "impersonated_by_name")
    op.drop_column("audit_logs", "impersonated_by")
