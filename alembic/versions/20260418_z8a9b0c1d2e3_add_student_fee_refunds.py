"""add student_fee_refunds table

新增學費退款歷史表，提供正式退款流程的稽核軌跡。
每次退款建立一筆紀錄，原 StudentFeeRecord.amount_paid 以「累計繳費 - 累計退款」重算。

Revision ID: z8a9b0c1d2e3
Revises: y7z8a9b0c1d2
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "z8a9b0c1d2e3"
down_revision = "y7z8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "student_fee_refunds" in tables:
        return
    if "student_fee_records" not in tables:
        # 基線尚未建立 fee records，安全跳過（測試資料庫可能缺該表）
        return

    op.create_table(
        "student_fee_refunds",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "record_id",
            sa.Integer,
            sa.ForeignKey("student_fee_records.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount", sa.Integer, nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.Column("notes", sa.Text, nullable=True, server_default=""),
        sa.Column("refunded_by", sa.String(length=50), nullable=False),
        sa.Column(
            "refunded_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_fee_refunds_record", "student_fee_refunds", ["record_id"])
    op.create_index(
        "ix_fee_refunds_refunded_at", "student_fee_refunds", ["refunded_at"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "student_fee_refunds" not in inspect(bind).get_table_names():
        return
    op.drop_index("ix_fee_refunds_refunded_at", table_name="student_fee_refunds")
    op.drop_index("ix_fee_refunds_record", table_name="student_fee_refunds")
    op.drop_table("student_fee_refunds")
