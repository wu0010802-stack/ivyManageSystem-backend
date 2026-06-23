"""activity_payment_records 新增 receipt_items_snapshot（收據明細開立當下快照）

Revision ID: aprsnap01
Revises: actvinstr01
Create Date: 2026-06-23

2026-06-23 audit Finding 2：POS 收據補印（print.pdf / 冪等 replay）原本即時查
RegistrationCourse / RegistrationSupply 重建明細，付款後若增退課/移用品，補印的
明細列會漂移成現況、與收據合計（來自不可變 payment record）對不上。

修法：checkout 開立收據時把整張收據的 items 顯示明細序列化存進「整張收據第一筆
（anchor）」紀錄的 receipt_items_snapshot（JSON, nullable）；補印優先讀此 immutable
snapshot。nullable 無預設，既有列為 NULL（此欄上線前的舊收據）→ 補印退回即時重建
並標註「明細依目前報名狀態重建」。

downgrade：drop_column receipt_items_snapshot。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "aprsnap01"
down_revision = "actvinstr01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "activity_payment_records",
        sa.Column("receipt_items_snapshot", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("activity_payment_records", "receipt_items_snapshot")
