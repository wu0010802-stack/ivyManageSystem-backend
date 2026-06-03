"""BonusConfig: 加入未打卡扣款費率 missing_punch_deduction_per_time

Revision ID: yeatpunch01
Revises: yeartcfg01
Create Date: 2026-06-02

說明（年終 E化 Phase 2 / B5）：
新增 bonus_configs.missing_punch_deduction_per_time（Float，nullable，
server_default="50"），對應年終「遲到一覽表」未打卡每次定額罰則 -50/次
（業主 2026-06-02 確認納入）。

注意：B5 同步把 model 端 late_deduction_per_time 的 default 從 100 改 50
（與 Excel -50/次 一致），但該欄 server_default 仍為 B1 migration 的 "100"。
現有 BonusConfig 列皆由 ORM 建立（套用 model default），且 B1 尚未部署，
故 model-only 已足；server_default 由業主決定是否一併調整。

downgrade 完整 drop 此欄。
"""

from alembic import op
import sqlalchemy as sa

revision = "yeatpunch01"
down_revision = "yeartcfg01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bonus_configs",
        sa.Column(
            "missing_punch_deduction_per_time",
            sa.Float(),
            nullable=True,
            server_default="50",
            comment="未打卡每次扣年終款（年終定額罰則，預設 50 元；Excel 遲到一覽表）",
        ),
    )


def downgrade() -> None:
    op.drop_column("bonus_configs", "missing_punch_deduction_per_time")
