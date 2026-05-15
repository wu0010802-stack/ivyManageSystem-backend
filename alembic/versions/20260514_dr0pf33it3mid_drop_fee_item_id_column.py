"""tuition refactor c3: drop student_fee_records.fee_item_id column

c2 已 DROP TABLE fee_items 並把 fee_item_id 變 nullable / 卸 FK 與 unique。
本 migration 把殘留的 fee_item_id column 與其 index 一併砍掉，完成範本驅動單軌。

保留 fee_item_name column（snapshot 用，未來歷史查詢仍可讀）。

upgrade：
1. DROP INDEX ix_fee_records_fee_item（若還在）
2. DROP COLUMN student_fee_records.fee_item_id

⚠️ DOWNGRADE 不可逆警告：
production 已執行 upgrade 後，downgrade 只能還原 schema（重建欄位 + index），
無法還原欄位內已 DROP 的歷史值（PostgreSQL DROP COLUMN 永久丟資料）。
事後重建只能得到全 NULL 的 fee_item_id 欄位。
若 production 仍有需要保留的 fee_item_id 對應關係，務必先：
  1. 從 audit log / 備份 dump 出 (student_fee_record_id, fee_item_id) 對應表
  2. upgrade 後若決定 downgrade，先重建欄位再從備份回填
本 downgrade 僅對稱還原 schema（ADD COLUMN nullable + 重建 INDEX；不需 FK
也不需 unique，因 c2 已卸除；fee_items 表本身由 tu1ti0nr3f4ct 的 downgrade 重建）。

Revision ID: dr0pf33it3mid
Revises: tu1ti0nr3f4ct
Create Date: 2026-05-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "dr0pf33it3mid"
down_revision = "tu1ti0nr3f4ct"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "student_fee_records" not in tables:
        return

    # ── 1) DROP INDEX ix_fee_records_fee_item（若還在）─────────────────
    existing_indexes = {
        idx.get("name") for idx in inspector.get_indexes("student_fee_records")
    }
    if "ix_fee_records_fee_item" in existing_indexes:
        op.drop_index("ix_fee_records_fee_item", table_name="student_fee_records")

    # ── 2) DROP COLUMN fee_item_id（若還在）────────────────────────────
    existing_columns = {
        col.get("name") for col in inspector.get_columns("student_fee_records")
    }
    if "fee_item_id" in existing_columns:
        op.drop_column("student_fee_records", "fee_item_id")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "student_fee_records" not in tables:
        return

    existing_columns = {
        col.get("name") for col in inspector.get_columns("student_fee_records")
    }

    # ── 2') ADD COLUMN fee_item_id（nullable，c2 已卸 FK 與 unique）─────
    if "fee_item_id" not in existing_columns:
        op.add_column(
            "student_fee_records",
            sa.Column("fee_item_id", sa.Integer(), nullable=True),
        )

    # ── 1') 重建 INDEX ix_fee_records_fee_item（若不存在）──────────────
    existing_indexes = {
        idx.get("name") for idx in inspector.get_indexes("student_fee_records")
    }
    if "ix_fee_records_fee_item" not in existing_indexes:
        op.create_index(
            "ix_fee_records_fee_item",
            "student_fee_records",
            ["fee_item_id"],
        )
