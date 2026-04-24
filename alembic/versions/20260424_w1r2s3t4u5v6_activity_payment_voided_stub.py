"""stub for reverted 'activity_payment_voided' migration

Revision ID: w1r2s3t4u5v6
Revises: v0q1r2s3t4u5
Create Date: 2026-04-24

背景：
  此 revision id 原本對應「才藝收費嚴格化 - ActivityPaymentRecord 加
  voided_at/voided_by/void_reason 軟刪欄位」的 migration，已被業主主動
  revert 掉（見 2026-04-24 的 project_activity_fee_strictness.md）。

  但部分環境的 PostgreSQL 已經跑過那筆 migration、DB 的 alembic_version
  仍記錄 'w1r2s3t4u5v6'，之後 code/migration 檔一起 revert 後，alembic
  每次啟動會找不到這個 revision 而 fail：
    `Can't locate revision identified by 'w1r2s3t4u5v6'`

  本檔是 placeholder：
  - upgrade() noop — 若環境已經建過這三欄，保留即可（code 不再讀寫，無害）
  - downgrade() 清理 voided_* 三欄（手動 downgrade 時才會觸發）

  之後所有新 migration 的 down_revision 都以此為基礎往前接，保持 linear
  revision chain。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "w1r2s3t4u5v6"
down_revision = "v0q1r2s3t4u5"
branch_labels = None
depends_on = None


_TABLE = "activity_payment_records"
_VOIDED_COLUMNS = ("voided_at", "voided_by", "void_reason")


def upgrade() -> None:
    # noop：原 migration 已被 revert，不再建欄位
    # （若歷史 DB 已經有這三欄，保留即可；不再有 code 讀寫）
    pass


def downgrade() -> None:
    """主動 downgrade 時清理歷史 DB 裡殘留的 voided_* 欄位。"""
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    for col in _VOIDED_COLUMNS:
        if col in existing:
            op.drop_column(_TABLE, col)
