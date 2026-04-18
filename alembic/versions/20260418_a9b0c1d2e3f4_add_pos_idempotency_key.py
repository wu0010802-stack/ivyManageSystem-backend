"""add idempotency_key column to activity_payment_records

POS 冪等查詢原本用 LIKE '%[IDK:xxx]%' 掃 notes 欄位：
- 無法走 index，交易量大時每次 checkout 都全表掃
- 使用者備註若含 [IDK:...] 字樣會干擾匹配

改為獨立欄位 + index，查詢改為等值比對，並把舊紀錄中的 [IDK:...]
標記回填到新欄位。

Revision ID: a9b0c1d2e3f4
Revises: z8a9b0c1d2e3
Create Date: 2026-04-18
"""

import re

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "a9b0c1d2e3f4"
down_revision = "z8a9b0c1d2e3"
branch_labels = None
depends_on = None


_IDK_RE = re.compile(r"\[IDK:([A-Za-z0-9_-]{8,64})\]")


def _existing_cols(bind, table: str) -> set:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def _existing_indexes(bind, table: str) -> set:
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "activity_payment_records" not in tables:
        return

    cols = _existing_cols(bind, "activity_payment_records")
    if "idempotency_key" not in cols:
        op.add_column(
            "activity_payment_records",
            sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        )

    idx = _existing_indexes(bind, "activity_payment_records")
    if "ix_activity_payment_records_idk_created" not in idx:
        op.create_index(
            "ix_activity_payment_records_idk_created",
            "activity_payment_records",
            ["idempotency_key", "created_at"],
        )

    # 回填舊紀錄：從 notes 解析 [IDK:...] 填入新欄位
    rows = bind.execute(
        sa.text(
            "SELECT id, notes FROM activity_payment_records "
            "WHERE idempotency_key IS NULL AND notes LIKE '%[IDK:%'"
        )
    ).fetchall()
    for row in rows:
        m = _IDK_RE.search(row.notes or "")
        if m:
            bind.execute(
                sa.text(
                    "UPDATE activity_payment_records SET idempotency_key = :k "
                    "WHERE id = :i"
                ),
                {"k": m.group(1), "i": row.id},
            )


def downgrade() -> None:
    bind = op.get_bind()
    if "activity_payment_records" not in inspect(bind).get_table_names():
        return

    idx = _existing_indexes(bind, "activity_payment_records")
    if "ix_activity_payment_records_idk_created" in idx:
        op.drop_index(
            "ix_activity_payment_records_idk_created",
            table_name="activity_payment_records",
        )

    cols = _existing_cols(bind, "activity_payment_records")
    if "idempotency_key" in cols:
        op.drop_column("activity_payment_records", "idempotency_key")
