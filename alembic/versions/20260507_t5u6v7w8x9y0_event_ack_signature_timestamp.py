"""event_acknowledgments 加 signature_uploaded_at 欄位（防 ack_deadline 後補簽）

威脅：
- 簽名圖上傳端點未檢查 event.ack_deadline；家長可在截止日後仍上傳簽名
- 簽名 attachment 本身無時序欄位；事後審計無法判斷「是否準時簽」、「重簽過幾次」

修法：加 signature_uploaded_at (DateTime, nullable)：
- upload_ack_signature 寫入時記 now()
- 後端在上傳前檢查 event.ack_deadline；過期則 400 拒絕
- 重簽會更新此欄位（與軟刪舊 attachment 對齊）
- 既有資料 NULL → 視為未紀錄（與 signature_attachment_id 為 NULL 對齊向後相容）

Refs: 資安掃描 2026-05-07 P2。

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
Create Date: 2026-05-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "t5u6v7w8x9y0"
down_revision = "s4t5u6v7w8x9"
branch_labels = None
depends_on = None


_TABLE = "event_acknowledgments"
_COL = "signature_uploaded_at"


def _column_exists(table: str, name: str) -> bool:
    bind = op.get_bind()
    insp = inspect(bind)
    cols = {c["name"] for c in insp.get_columns(table)}
    return name in cols


def upgrade() -> None:
    if not _column_exists(_TABLE, _COL):
        op.add_column(
            _TABLE,
            sa.Column(
                _COL,
                sa.DateTime(),
                nullable=True,
                comment="簽名圖上傳時間（防 ack_deadline 後補簽 + 重簽紀錄）",
            ),
        )


def downgrade() -> None:
    if _column_exists(_TABLE, _COL):
        op.drop_column(_TABLE, _COL)
