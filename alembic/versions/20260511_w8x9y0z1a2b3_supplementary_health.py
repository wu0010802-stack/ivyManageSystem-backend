"""InsuranceRate 加二代健保補充保費欄位（兼職薪資 ≥ 門檻時扣 2.11%）

業務規則：
- Excel 註記「115.01 月起未達 29500 元，兼職所得無需扣除二代健保」
- 適用 employee_type='hourly'（才藝/鐘點老師）
- 門檻：當期基本工資（預設 29500，可調整）
- 費率：補充保費率（預設 2.11% = 一般 1.91% + 補充 0.20%）

Revision ID: w8x9y0z1a2b3
Revises: v7w8x9y0z1a2
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "w8x9y0z1a2b3"
down_revision = "v7w8x9y0z1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "insurance_rates" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("insurance_rates")}

    if "supplementary_health_rate" not in cols:
        op.add_column(
            "insurance_rates",
            sa.Column(
                "supplementary_health_rate",
                sa.Float(),
                nullable=False,
                server_default=sa.text("0.0211"),
                comment="二代健保補充保費率（兼職單筆給付 ≥ 門檻時適用）",
            ),
        )
    if "supplementary_health_threshold" not in cols:
        op.add_column(
            "insurance_rates",
            sa.Column(
                "supplementary_health_threshold",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("29500"),
                comment="補充保費起扣門檻（兼職單筆給付達此金額才扣，現行基本工資 29500）",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "insurance_rates" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("insurance_rates")}
    for col_name in ("supplementary_health_threshold", "supplementary_health_rate"):
        if col_name in cols:
            op.drop_column("insurance_rates", col_name)
