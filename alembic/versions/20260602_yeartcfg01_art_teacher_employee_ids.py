"""BonusConfig: 加入才藝老師年終收款人欄位 art_teacher_employee_ids

Revision ID: yeartcfg01
Revises: bonuscfg_p2
Create Date: 2026-06-02

說明：
新增 bonus_configs.art_teacher_employee_ids（JSON list of employee id，nullable，
無 server_default＝NULL 未設定）。

語意：每位列名才藝老師年終得「全校總人次 × art_teacher_unit_price」。
NULL / 空 list → 未指定才藝老師，年終才藝老師段跳過（不報錯）。

downgrade 完整 drop 此欄。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "yeartcfg01"
down_revision = "bonuscfg_p2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bonus_configs",
        sa.Column(
            "art_teacher_employee_ids",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
            comment="才藝老師年終收款人 employee id list（JSON，NULL/空=未指定）",
        ),
    )


def downgrade() -> None:
    op.drop_column("bonus_configs", "art_teacher_employee_ids")
