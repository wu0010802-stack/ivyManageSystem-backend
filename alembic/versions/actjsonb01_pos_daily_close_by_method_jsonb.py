"""POS 日結 by_method_json Text→JSONB

Revision ID: actjsonb01
Revises: actcons01
Create Date: 2026-06-23

Why（2026-06-23 優化盤點 資料模型一致性）:

ActivityPosDailyClose / ActivityPosDailyCloseHistory 的 by_method_json 原以 Text
存 JSON 字串，每個 caller 手動 json.loads/json.dumps；同檔
ActivityPaymentRecord.receipt_items_snapshot 已用 JSON type，同一檔兩種 JSON 存法
不一致。改用 JSONB：ORM 自動序列化，移除 pos_approval.py 四處手動 json 樣板，
並由 DB 保證內容為合法 JSON。

dev DB 既有資料全部可 cast（3 列 close 皆合法 JSON、history 0 列），ALTER TYPE
... USING by_method_json::jsonb 安全。column 無 server_default（default="{}" 為
ORM Python-side），故無 DEFAULT 需轉換。

models/activity.py 已同步兩欄 Column(Text→JSON, default="{}"→dict)。SQLite 測試
DB 的 JSON type 以 TEXT 落地、ORM 端仍 dict in/out，行為一致。

SQLite：JSON type 由 metadata 直接建表，本 migration（ALTER TYPE）僅 PostgreSQL
執行。downgrade：JSONB→text（::text cast，產生 compact JSON 字串）。
"""

import logging

from alembic import op

logger = logging.getLogger(__name__)

revision = "actjsonb01"
down_revision = "actcons01"
branch_labels = None
depends_on = None

_TABLES = ("activity_pos_daily_close", "activity_pos_daily_close_history")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        logger.info(
            "非 PostgreSQL（%s），跳過 by_method_json JSONB 轉型", bind.dialect.name
        )
        return
    for table in _TABLES:
        # column 有 server default '{}'::text，無法隨 type 自動 cast → 先 DROP、
        # 轉型後再以 jsonb 重設（保留原 server-default 行為）。
        op.execute(f"ALTER TABLE {table} ALTER COLUMN by_method_json DROP DEFAULT")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN by_method_json "
            "TYPE jsonb USING by_method_json::jsonb"
        )
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN by_method_json "
            "SET DEFAULT '{}'::jsonb"
        )
        logger.info("已轉型 %s.by_method_json → jsonb", table)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN by_method_json DROP DEFAULT")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN by_method_json "
            "TYPE text USING by_method_json::text"
        )
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN by_method_json " "SET DEFAULT '{}'::text"
        )
