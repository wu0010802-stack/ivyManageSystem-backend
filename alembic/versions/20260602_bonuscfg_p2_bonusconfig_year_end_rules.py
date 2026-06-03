"""BonusConfig: 加入年終 E化 Phase 2 規則欄位

Revision ID: bonuscfg_p2
Revises: studnum01
Create Date: 2026-06-02

說明：
新增 bonus_configs 表下列欄位（全部 nullable）：

含 server_default（現有資料列升級後自動填入預設值）：
  - dividend_returning_threshold Float  紅利舊生率門檻（預設 0.9）
  - dividend_returning_amount    Float  舊生率門檻達標獎金（預設 500）
  - dividend_activity_threshold  Float  紅利才藝率門檻（預設 0.8）
  - dividend_activity_amount     Float  才藝率門檻達標獎金（預設 1000）
  - late_deduction_per_time      Float  遲到每次扣年終款（預設 100）
  - personal_leave_deduction_per_day Float  事假每日扣年終款（預設 500）
  - sick_leave_deduction_per_day Float  病假每日扣年終款（預設 500）

無 server_default（NULL=尚未設定，HR 需在設定頁填入）：
  - art_teacher_unit_price       Float  才藝老師課時單價
  - after_class_award_unit_price JSON   課後才藝班年終單價（班名→K 單價）

downgrade 完整 drop 上述 9 欄。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "bonuscfg_p2"
down_revision = "studnum01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bonus_configs",
        sa.Column(
            "art_teacher_unit_price",
            sa.Float(),
            nullable=True,
            comment="才藝老師每課時單價（年終計算用，NULL=未設定）",
        ),
    )
    op.add_column(
        "bonus_configs",
        sa.Column(
            "dividend_returning_threshold",
            sa.Float(),
            nullable=True,
            server_default="0.9",
            comment="紅利舊生率門檻（預設 0.9 = 90%）",
        ),
    )
    op.add_column(
        "bonus_configs",
        sa.Column(
            "dividend_returning_amount",
            sa.Float(),
            nullable=True,
            server_default="500",
            comment="達到舊生率門檻的紅利獎金金額（預設 500）",
        ),
    )
    op.add_column(
        "bonus_configs",
        sa.Column(
            "dividend_activity_threshold",
            sa.Float(),
            nullable=True,
            server_default="0.8",
            comment="紅利才藝參與率門檻（預設 0.8 = 80%）",
        ),
    )
    op.add_column(
        "bonus_configs",
        sa.Column(
            "dividend_activity_amount",
            sa.Float(),
            nullable=True,
            server_default="1000",
            comment="達到才藝率門檻的紅利獎金金額（預設 1000）",
        ),
    )
    op.add_column(
        "bonus_configs",
        sa.Column(
            "late_deduction_per_time",
            sa.Float(),
            nullable=True,
            server_default="100",
            comment="遲到每次扣年終款（預設 100 元）",
        ),
    )
    op.add_column(
        "bonus_configs",
        sa.Column(
            "personal_leave_deduction_per_day",
            sa.Float(),
            nullable=True,
            server_default="500",
            comment="事假每日扣年終款（預設 500 元）",
        ),
    )
    op.add_column(
        "bonus_configs",
        sa.Column(
            "sick_leave_deduction_per_day",
            sa.Float(),
            nullable=True,
            server_default="500",
            comment="病假每日扣年終款（預設 500 元）",
        ),
    )
    op.add_column(
        "bonus_configs",
        sa.Column(
            "after_class_award_unit_price",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
            comment="課後才藝班年終單價 JSON（班名→K 單價，NULL=未設定）",
        ),
    )


def downgrade() -> None:
    op.drop_column("bonus_configs", "after_class_award_unit_price")
    op.drop_column("bonus_configs", "sick_leave_deduction_per_day")
    op.drop_column("bonus_configs", "personal_leave_deduction_per_day")
    op.drop_column("bonus_configs", "late_deduction_per_time")
    op.drop_column("bonus_configs", "dividend_activity_amount")
    op.drop_column("bonus_configs", "dividend_activity_threshold")
    op.drop_column("bonus_configs", "dividend_returning_amount")
    op.drop_column("bonus_configs", "dividend_returning_threshold")
    op.drop_column("bonus_configs", "art_teacher_unit_price")
