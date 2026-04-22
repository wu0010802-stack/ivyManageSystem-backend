"""add receipt_no column to activity_payment_records

Revision ID: v0q1r2s3t4u5
Revises: u9p0q1r2s3t4
Create Date: 2026-04-22

Why:
  先前 POS 結帳查詢同一張收據的所有 items / 碰撞檢測都用 `notes LIKE '%[POS-...]%'`
  模糊比對。notes 是使用者可見備註欄（200 字上限），使用者理論上可在備註塞
  偽造 `[POS-YYYYMMDD-xxxx]` 字串干擾比對；且 LIKE 走不了普通索引。

  本 migration 把 receipt_no 獨立成欄位 + index，回填歷史資料，後續寫入時一律
  填 receipt_no 欄位。notes 仍保留 `[POS-...]` 標記供舊版解析相容，但查詢已不再
  依賴。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "v0q1r2s3t4u5"
down_revision = "u9p0q1r2s3t4"
branch_labels = None
depends_on = None


_TABLE = "activity_payment_records"
_COLUMN = "receipt_no"
_INDEX = "ix_activity_payment_records_receipt_no"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in existing_cols:
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(length=40), nullable=True),
        )

    # Backfill：從 notes 抽出 `[POS-YYYYMMDD-hex]` 標記
    # PostgreSQL 用 substring + regex；SQLite 測試環境沒有 regex，跳過 backfill
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(sa.text("""
                UPDATE activity_payment_records
                   SET receipt_no = substring(
                           notes from '\\[(POS-\\d{8}-[A-Fa-f0-9]+)\\]'
                       )
                 WHERE receipt_no IS NULL
                   AND notes LIKE '%[POS-%'
                """))

    existing_indexes = {i["name"] for i in inspector.get_indexes(_TABLE)}
    if _INDEX not in existing_indexes:
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    existing_indexes = {i["name"] for i in inspector.get_indexes(_TABLE)}
    if _INDEX in existing_indexes:
        op.drop_index(_INDEX, table_name=_TABLE)

    existing_cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in existing_cols:
        op.drop_column(_TABLE, _COLUMN)
