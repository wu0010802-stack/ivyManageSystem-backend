"""add voided_at/voided_by/void_reason to activity_payment_records

Revision ID: w1r2s3t4u5v6
Revises: v0q1r2s3t4u5
Create Date: 2026-04-24

Why:
  DELETE payment 端點過去直接 session.delete()，員工可以「POS 收現金 →
  DELETE payment → paid_amount 重算歸零 → 私吞現金」，雖有 audit log 但
  原 payment row 已消失，稽核難以還原真相。

  本 migration 把 DELETE 改為軟刪除（voided），強制保留原紀錄並記錄執行者
  與原因，後續 paid_amount / daily summary 重算時以 voided_at IS NULL 為
  前提排除軟刪項目。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "w1r2s3t4u5v6"
down_revision = "v0q1r2s3t4u5"
branch_labels = None
depends_on = None


_TABLE = "activity_payment_records"
_NEW_COLUMNS = ("voided_at", "voided_by", "void_reason")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if "voided_at" not in existing_cols:
        op.add_column(
            _TABLE,
            sa.Column("voided_at", sa.DateTime(), nullable=True),
        )
    if "voided_by" not in existing_cols:
        op.add_column(
            _TABLE,
            sa.Column("voided_by", sa.String(length=50), nullable=True),
        )
    if "void_reason" not in existing_cols:
        op.add_column(
            _TABLE,
            sa.Column("void_reason", sa.String(length=200), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    for col in _NEW_COLUMNS:
        if col in existing_cols:
            op.drop_column(_TABLE, col)
