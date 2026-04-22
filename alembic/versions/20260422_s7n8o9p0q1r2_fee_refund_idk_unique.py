"""add idempotency_key + UniqueConstraint on student_fee_refunds

Revision ID: s7n8o9p0q1r2
Revises: r6m7n8o9p0q1
Create Date: 2026-04-22

Why:
  api/fees.py refund_fee_record 先 SELECT（已繳 >= 退款金額） 再 INSERT，
  網路重送同一請求會建兩筆 StudentFeeRefund 並雙扣 amount_paid。與才藝模組
  ActivityPaymentRecord 的 idempotency_key 策略一致：
    - 欄位：idempotency_key String(64) nullable
    - NULL 允許重複（舊資料相容）
    - 非 NULL 以 UniqueConstraint 攔下並發第二筆
    - 路由層補查詢視窗（10 分鐘）做 replay

歷史資料：
  此欄位為新增，既有紀錄全部為 NULL，不需清理重複。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "s7n8o9p0q1r2"
down_revision = "r6m7n8o9p0q1"
branch_labels = None
depends_on = None


_TABLE = "student_fee_refunds"
_COLUMN = "idempotency_key"
_UNIQUE_NAME = "uq_student_fee_refunds_idk"
_INDEX_NAME = "ix_fee_refunds_idk_refunded"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in cols:
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                sa.String(length=64),
                nullable=True,
                comment="退款冪等鍵（10 分鐘視窗內同 key 視為重試）",
            ),
        )

    existing_indexes = {ix["name"] for ix in inspector.get_indexes(_TABLE)}
    if _INDEX_NAME not in existing_indexes:
        op.create_index(
            _INDEX_NAME,
            _TABLE,
            [_COLUMN, "refunded_at"],
        )

    existing_uqs = {uq.get("name") for uq in inspector.get_unique_constraints(_TABLE)}
    if _UNIQUE_NAME not in existing_uqs:
        op.create_unique_constraint(
            _UNIQUE_NAME,
            _TABLE,
            [_COLUMN],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    existing_uqs = {uq.get("name") for uq in inspector.get_unique_constraints(_TABLE)}
    if _UNIQUE_NAME in existing_uqs:
        op.drop_constraint(_UNIQUE_NAME, _TABLE, type_="unique")

    existing_indexes = {ix["name"] for ix in inspector.get_indexes(_TABLE)}
    if _INDEX_NAME in existing_indexes:
        op.drop_index(_INDEX_NAME, table_name=_TABLE)

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in cols:
        op.drop_column(_TABLE, _COLUMN)
