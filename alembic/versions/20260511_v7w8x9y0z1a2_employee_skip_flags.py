"""employees 加「不薪轉」與「不入稅報」兩個獨立旗標

Why: 義華薪資 115.04 對齊發現兩種特殊個案，原本只有 skip_payroll_bonuses 一個旗標
無法區分：
- 吳逸喬「7/3執行長說不薪轉.支出不作帳」→ 薪資仍要算，但不入銀行轉帳名冊、不入稅報
- 吳逸倫「總園長說國稅局不作帳」→ 薪資+保險仍算，但所得不報國稅局

skip_payroll_bonuses（既有）只跳過獎金計算，與本兩需求語意不同，必須拆開。

Revision ID: v7w8x9y0z1a2
Revises: u6v7w8x9y0z1
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "v7w8x9y0z1a2"
down_revision = "u6v7w8x9y0z1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "employees" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("employees")}

    if "skip_payroll_transfer" not in cols:
        op.add_column(
            "employees",
            sa.Column(
                "skip_payroll_transfer",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
                comment=(
                    "不入銀行轉帳名冊（薪資仍計算，但用現金/其他管道支付，"
                    "如執行長指示不薪轉的個案）"
                ),
            ),
        )

    if "unreported_for_tax" not in cols:
        op.add_column(
            "employees",
            sa.Column(
                "unreported_for_tax",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
                comment=(
                    "所得不報國稅局（薪資、保險仍正常計算扣繳，但年度扣繳憑單匯出時排除）"
                ),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "employees" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("employees")}

    for col_name in ("unreported_for_tax", "skip_payroll_transfer"):
        if col_name in cols:
            op.drop_column("employees", col_name)
