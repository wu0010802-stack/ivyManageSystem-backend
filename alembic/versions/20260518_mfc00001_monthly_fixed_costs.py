"""monthly_fixed_costs: 月度固定費用（手動登錄）

Phase 2 新增表：放租金 / 辦公室零用金 / 廚房零用金 / 餐點 / 水費 / 電費 /
電話費 / 舊制勞退準備金（每月一筆 by category）。月度損益表 aggregator
讀取本表組裝「變動支出」7 條與「人事支出」舊制勞退列。

唯一鍵 (year, month, category) 確保每月每類別只有一筆；前端 batch upsert
時依此 key 做 INSERT ... ON CONFLICT。

Revision ID: mfc00001
Revises: vndpay01
Create Date: 2026-05-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "mfc00001"
down_revision = "vndpay01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monthly_fixed_costs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("month", sa.Integer, nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_by_id",
            sa.Integer,
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by_id",
            sa.Integer,
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "year", "month", "category", name="uq_monthly_fixed_costs_period_cat"
        ),
        sa.CheckConstraint(
            "month BETWEEN 1 AND 12", name="ck_monthly_fixed_costs_month"
        ),
        sa.CheckConstraint("amount >= 0", name="ck_monthly_fixed_costs_amount_nonneg"),
        sa.CheckConstraint(
            "category IN ('rent','office_petty_cash','kitchen_petty_cash','meals',"
            "'water','electricity','phone','old_pension_reserve')",
            name="ck_monthly_fixed_costs_category",
        ),
    )
    op.create_index(
        "ix_monthly_fixed_costs_year",
        "monthly_fixed_costs",
        ["year"],
    )
    op.create_index(
        "ix_monthly_fixed_costs_year_month",
        "monthly_fixed_costs",
        ["year", "month"],
    )


def downgrade() -> None:
    op.drop_index("ix_monthly_fixed_costs_year_month", table_name="monthly_fixed_costs")
    op.drop_index("ix_monthly_fixed_costs_year", table_name="monthly_fixed_costs")
    op.drop_table("monthly_fixed_costs")
