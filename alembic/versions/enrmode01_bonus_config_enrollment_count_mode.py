"""BonusConfig.enrollment_count_mode — 在籍人數計算模式（L3）

month_end（預設，月底單日快照＝既有語意）/ daily_weighted（按日加權平均）。
server_default 確保既有列回填 month_end，零漂移。

Refs: docs/superpowers/specs/2026-06-13-enrollment-count-correctness-design.md
Revision ID: enrmode01
Revises: enrsnap01
"""

import sqlalchemy as sa
from alembic import op

revision = "enrmode01"
down_revision = "enrsnap01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bonus_configs",
        sa.Column(
            "enrollment_count_mode",
            sa.String(length=20),
            nullable=False,
            server_default="month_end",
        ),
    )


def downgrade() -> None:
    op.drop_column("bonus_configs", "enrollment_count_mode")
