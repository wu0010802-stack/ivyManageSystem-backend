"""add probation_end_date to employees

為 employees 表補充試用期結束日欄位（若 DB 尚未存在則新增）

Revision ID: s6t7u8v9w0x1
Revises: r5s6t7u8v9w0
Create Date: 2026-03-20 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "s6t7u8v9w0x1"
down_revision = "r5s6t7u8v9w0"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("employees")}
    if "probation_end_date" not in cols:
        op.add_column(
            "employees",
            sa.Column("probation_end_date", sa.Date(), nullable=True, comment="試用期結束日"),
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("employees")}
    if "probation_end_date" in cols:
        op.drop_column("employees", "probation_end_date")
