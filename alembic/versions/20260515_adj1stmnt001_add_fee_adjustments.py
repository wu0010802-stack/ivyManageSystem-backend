"""add student_fee_adjustments table

Revision ID: adj1stmnt001
Revises: ar1n3c1d4l1nk
Create Date: 2026-05-15

新增「學費折抵」表，用於同胞優惠 / 預繳 / 請假扣款 / 其他「減少應收」的記錄。
獨立於 StudentFeeRecord（強制正金額）以保留現有 payment/refund 不變式。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "adj1stmnt001"
down_revision: Union[str, Sequence[str], None] = "ar1n3c1d4l1nk"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "student_fee_adjustments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False, comment="對應學生"),
        sa.Column("period", sa.String(length=20), nullable=False, comment="學期，如 114-2"),
        sa.Column(
            "adjustment_type",
            sa.String(length=50),
            nullable=False,
            comment="sibling_discount/prepayment/leave_deduction/other",
        ),
        sa.Column(
            "amount",
            sa.Integer(),
            nullable=False,
            comment="折抵金額（正整數，套用時相減）",
        ),
        sa.Column("reason", sa.String(length=200), nullable=True, comment="折抵原因說明"),
        sa.Column("notes", sa.Text(), nullable=True, comment="備註"),
        sa.Column("created_by", sa.String(length=50), nullable=True, comment="建立者 username"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["students.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "amount > 0",
            name="ck_fee_adjustments_amount_positive",
        ),
    )
    op.create_index(
        "ix_fee_adjustments_student_period",
        "student_fee_adjustments",
        ["student_id", "period"],
    )
    op.create_index(
        "ix_fee_adjustments_type",
        "student_fee_adjustments",
        ["adjustment_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_fee_adjustments_type", table_name="student_fee_adjustments")
    op.drop_index(
        "ix_fee_adjustments_student_period",
        table_name="student_fee_adjustments",
    )
    op.drop_table("student_fee_adjustments")
