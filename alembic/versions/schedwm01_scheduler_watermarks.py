"""scheduler_watermarks：排程器時間游標持久化（修復重啟漏推排程公告）

Revision ID: schedwm01
Revises: studnum01
Create Date: 2026-06-03

新增 scheduler_watermarks 表存放排程器時間游標。announcement publish
scheduler 原本把游標只存記憶體，重啟即重置成 now() → 重啟/部署窗口內
排程的公告永久漏推。此表讓游標跨重啟存活。

無 backfill：首次無列時 scheduler fallback now()，與舊行為一致、不重推歷史。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "schedwm01"
down_revision: Union[str, Sequence[str], None] = "studnum01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduler_watermarks",
        sa.Column("name", sa.String(length=64), primary_key=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("scheduler_watermarks")
