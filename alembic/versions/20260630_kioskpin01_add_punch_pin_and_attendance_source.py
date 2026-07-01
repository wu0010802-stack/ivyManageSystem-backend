"""add punch_pin to employees and source to attendances

Revision ID: kioskpin01
Revises: enrterm01
Create Date: 2026-06-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "kioskpin01"
down_revision = "enrterm01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    emp_cols = {c["name"] for c in inspector.get_columns("employees")}
    if "punch_pin_hash" not in emp_cols:
        op.add_column(
            "employees",
            sa.Column(
                "punch_pin_hash",
                sa.String(length=200),
                nullable=True,
                comment="打卡 PIN 雜湊（PBKDF2，明文不落庫）",
            ),
        )
    if "punch_pin_set_at" not in emp_cols:
        op.add_column(
            "employees",
            sa.Column(
                "punch_pin_set_at",
                sa.DateTime(),
                nullable=True,
                comment="打卡 PIN 設定/重置時間",
            ),
        )

    att_cols = {c["name"] for c in inspector.get_columns("attendances")}
    if "source" not in att_cols:
        op.add_column(
            "attendances",
            sa.Column(
                "source",
                sa.String(length=20),
                nullable=True,
                comment="打卡來源：kiosk/manual/import；NULL=歷史未知",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    att_cols = {c["name"] for c in inspector.get_columns("attendances")}
    if "source" in att_cols:
        op.drop_column("attendances", "source")

    emp_cols = {c["name"] for c in inspector.get_columns("employees")}
    if "punch_pin_set_at" in emp_cols:
        op.drop_column("employees", "punch_pin_set_at")
    if "punch_pin_hash" in emp_cols:
        op.drop_column("employees", "punch_pin_hash")
