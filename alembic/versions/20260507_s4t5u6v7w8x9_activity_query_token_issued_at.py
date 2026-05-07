"""activity_registrations 加 query_token_issued_at 欄位（公開查詢碼到期判定用）

威脅：Phase 3 加的公開查詢碼 (`query_token_hash`) 沒有時效性欄位，token 一旦發出永
久有效。家長截圖、LINE 轉傳、換手機後二手釋出，任何人配上 phone 都能查全家
報名（phone 雙因素，但 phone 變動不同步）。

修法：加 `query_token_issued_at` (DateTime, nullable)：
- register 端點寫 hash 時同時記 issued_at = now()
- public_query_by_token 驗證 now - issued_at < TTL（env ACTIVITY_QUERY_TOKEN_TTL_DAYS，預設 180）
- reject rotate 把 hash 設 None 時清 issued_at

向後相容：欄位 nullable，既有 reg 維持 NULL → 查詢端點視為已過期（強制走 /public/query 三欄比對）。
不影響家長體驗：未過期的 token 仍可用，過期的引導改用備援查詢。

Refs: 資安掃描 2026-05-07 P0。

Revision ID: s4t5u6v7w8x9
Revises: q2r3s4t5u6v7
Create Date: 2026-05-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "s4t5u6v7w8x9"
down_revision = "q2r3s4t5u6v7"
branch_labels = None
depends_on = None


_TABLE = "activity_registrations"
_COL = "query_token_issued_at"


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
                comment="公開查詢碼簽發時間（過期判定用）",
            ),
        )


def downgrade() -> None:
    if _column_exists(_TABLE, _COL):
        op.drop_column(_TABLE, _COL)
