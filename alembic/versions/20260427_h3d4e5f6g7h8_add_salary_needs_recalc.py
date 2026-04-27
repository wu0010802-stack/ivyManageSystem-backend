"""SalaryRecord 加 needs_recalc 旗標 + index

修正 advisor 標記的 P1:
- 批次重算單筆失敗仍 commit 其他人,失敗員工 SalaryRecord 留下舊值,
  之後 finalize 完整性檢查只看 row 存在 → 舊薪資被誤封存
- 假單 / 加班審核先 commit 再呼叫薪資重算,重算失敗只回 salary_warning,
  封存時無法察覺薪資已 stale

新欄位 needs_recalc:
- 重算成功 → False
- 批次重算 except 路徑 / 假單&加班審核降級 → True
- finalize 完整性檢查擴充為「missing 或 needs_recalc=True」一律拒絕

Revision ID: h3d4e5f6g7h8
Revises: g2c3d4e5f6g7
Create Date: 2026-04-27
"""

import sqlalchemy as sa
from alembic import op

revision = "h3d4e5f6g7h8"
down_revision = "g2c3d4e5f6g7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "salary_records",
        sa.Column(
            "needs_recalc",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "True 表示最後一次重算失敗或上游審核變動後未成功重算;"
                "封存時必須為 False"
            ),
        ),
    )
    op.create_index(
        "ix_salary_ym_needs_recalc",
        "salary_records",
        ["salary_year", "salary_month", "needs_recalc"],
    )


def downgrade():
    op.drop_index("ix_salary_ym_needs_recalc", table_name="salary_records")
    op.drop_column("salary_records", "needs_recalc")
