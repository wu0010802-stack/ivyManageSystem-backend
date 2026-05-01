"""才藝公開查詢 Phase 3 — activity_registrations 加 query_token_hash

Revision ID: o0l1m2n3o4p5
Revises: n9k0l1m2n3o4
Create Date: 2026-05-01

家長報名後拿到一個 32-char URL-safe 明文 token（response 一次性回，不存 DB），
DB 只存 HMAC-SHA256(JWT_SECRET_KEY, domain || token) 的 hex digest（64 chars）。
hash 存進此欄位後可作為 /public/query-by-token 的反查鍵。
- nullable=True：既有報名沒有 token，沿用三欄查詢
- index=True：query-by-token 端點需 hash 反查
- 不做 unique：rotate 時新舊不衝突，且不同 reg hash 撞機率近 0
"""

import sqlalchemy as sa
from alembic import op

revision = "o0l1m2n3o4p5"
down_revision = "n9k0l1m2n3o4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "activity_registrations",
        sa.Column("query_token_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_activity_regs_query_token_hash",
        "activity_registrations",
        ["query_token_hash"],
    )


def downgrade():
    op.drop_index(
        "ix_activity_regs_query_token_hash", table_name="activity_registrations"
    )
    op.drop_column("activity_registrations", "query_token_hash")
