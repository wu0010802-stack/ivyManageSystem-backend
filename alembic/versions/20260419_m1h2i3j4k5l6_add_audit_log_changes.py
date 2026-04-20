"""add changes column to audit_logs

Revision ID: m1h2i3j4k5l6
Revises: l0g1h2i3j4k5
Create Date: 2026-04-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "m1h2i3j4k5l6"
down_revision = "l0g1h2i3j4k5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns("audit_logs")}
    if "changes" in cols:
        return
    op.add_column(
        "audit_logs",
        sa.Column("changes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns("audit_logs")}
    if "changes" not in cols:
        return
    op.drop_column("audit_logs", "changes")
