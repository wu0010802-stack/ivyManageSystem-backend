"""SalaryRecord 加 manual_overrides 欄位(JSON 清單)

Issue 6 修補:manual_adjust_salary 直接寫到 SalaryRecord 本體欄位,後續上游
事件(假單/加班/補卡/設定改版)觸發重算時 _fill_salary_record 會無條件
覆寫 performance/special/festival/扣款等欄位,人工調整被吃掉。

修法:
- 新欄位 manual_overrides 紀錄被 manual_adjust 寫過的欄位名單
- manual_adjust 寫欄位後將欄位名加入此清單
- _fill_salary_record 重算時跳過清單內欄位,只更新其他欄位+從 record 重算
  gross/total_deduction/net 等聚合,確保 totals 與被保留的人工值一致

JSON nullable=True 是為了與既有資料相容(讀時以 `or []` 防 None);新建 record
透過 ORM default=list 寫成 [],與 needs_recalc 的 not-null + default 一樣安全。

Revision ID: j5g6h7i8j9k0
Revises: i4e5f6g7h8i9
Create Date: 2026-04-28
"""

import sqlalchemy as sa
from alembic import op


revision = "j5g6h7i8j9k0"
down_revision = "i4e5f6g7h8i9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "salary_records",
        sa.Column(
            "manual_overrides",
            sa.JSON(),
            nullable=True,
            comment=(
                "被 manual_adjust_salary 寫過的欄位名稱清單。重算時 "
                "_fill_salary_record 會跳過清單內的欄位,避免上游事件觸發的"
                "自動重算覆蓋人工調整。"
            ),
        ),
    )


def downgrade():
    op.drop_column("salary_records", "manual_overrides")
