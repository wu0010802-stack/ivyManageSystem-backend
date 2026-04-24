"""create student_fee_payments (append-only fee collection log) + backfill

Revision ID: w2s3t4u5v6w7
Revises: v0q1r2s3t4u5
Create Date: 2026-04-24

Why:
  StudentFeeRecord 原本只保留單一 amount_paid/payment_date/status 快照，
  pay_fee_record 每次收款直接覆寫。分期繳費會把收入搬到最後一次付款的
  月份，退款後月份可能整筆消失，partial 狀態的現金完全不入帳。

  改為 append-only 的 StudentFeePayment 流水表：每次 pay 插入一筆紀錄，
  財務月報從此表聚合。既有 StudentFeeRecord 中 amount_paid > 0 的紀錄，
  自動回填一筆初始 StudentFeePayment 以保留歷史（使用 record 上現有的
  payment_date / payment_method 作為 snapshot）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "w2s3t4u5v6w7"
# 接在 w1r2s3t4u5v6 stub 之後而非直接接 v0q1r2s3t4u5，避免 multiple heads。
# w1r2s3t4u5v6 是已 revert 的才藝嚴格化 migration 的 placeholder（見該檔說明）。
down_revision = "w1r2s3t4u5v6"
branch_labels = None
depends_on = None


_TABLE = "student_fee_payments"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        op.create_table(
            _TABLE,
            sa.Column(
                "id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False
            ),
            sa.Column(
                "record_id",
                sa.Integer(),
                sa.ForeignKey("student_fee_records.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("payment_date", sa.Date(), nullable=False),
            sa.Column("payment_method", sa.String(length=20), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("operator", sa.String(length=50), nullable=True),
            sa.Column("idempotency_key", sa.String(length=64), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index("ix_fee_payments_record", _TABLE, ["record_id"])
        op.create_index("ix_fee_payments_date", _TABLE, ["payment_date"])
        op.create_index(
            "ix_fee_payments_record_date", _TABLE, ["record_id", "payment_date"]
        )
        op.create_index("ix_fee_payments_idk", _TABLE, ["idempotency_key"])
        op.create_unique_constraint(
            "uq_student_fee_payments_idk", _TABLE, ["idempotency_key"]
        )

    # Backfill：既有 amount_paid > 0 的 StudentFeeRecord 補一筆初始 payment
    # 保留 payment_date（NULL 用 created_at::date 兜底）與 payment_method
    op.execute(sa.text("""
            INSERT INTO student_fee_payments
                (record_id, amount, payment_date, payment_method, notes, operator, created_at)
            SELECT
                r.id,
                r.amount_paid,
                COALESCE(r.payment_date, DATE(r.created_at)),
                r.payment_method,
                '（migration 回填既有 amount_paid 快照）',
                'system',
                COALESCE(r.updated_at, r.created_at, CURRENT_TIMESTAMP)
            FROM student_fee_records r
            WHERE (r.amount_paid IS NOT NULL AND r.amount_paid > 0)
              AND NOT EXISTS (
                SELECT 1 FROM student_fee_payments p WHERE p.record_id = r.id
              )
            """))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE in inspector.get_table_names():
        op.drop_table(_TABLE)
