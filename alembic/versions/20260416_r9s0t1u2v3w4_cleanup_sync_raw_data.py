"""cleanup sync_raw_data

清理已完成同步的暫存原始資料（教育部爬蟲暫存），釋放磁碟空間。
sync_raw_data 存放一次性爬蟲結果，同步完成後原始資料已寫入 competitor_school，
超過 30 天的暫存資料可安全清除。

Revision ID: r9s0t1u2v3w4
Revises: q8r9s0t1u2v3
Create Date: 2026-04-16 00:00:00.000000
"""

from alembic import op
from sqlalchemy import text, inspect

revision = "r9s0t1u2v3w4"
down_revision = "q8r9s0t1u2v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "sync_raw_data" not in tables:
        return

    result = bind.execute(
        text("DELETE FROM sync_raw_data WHERE created_at < NOW() - INTERVAL '30 days'")
    )
    deleted = result.rowcount
    if deleted > 0:
        # VACUUM 需要在 transaction 外執行，alembic 無法直接做
        # 交由 autovacuum 處理即可
        pass


def downgrade() -> None:
    # 資料清理不可逆，downgrade 為空操作
    pass
