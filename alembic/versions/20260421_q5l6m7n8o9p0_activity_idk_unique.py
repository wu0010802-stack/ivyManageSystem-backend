"""add unique constraint on activity_payment_records.idempotency_key

Revision ID: q5l6m7n8o9p0
Revises: p4k5l6m7n8o9
Create Date: 2026-04-21

Why:
  原本 idempotency_key 只是普通 Index，並發重送時「先 SELECT 再 INSERT」的冪等判斷
  會兩邊都查不到舊記錄而雙寫（POS checkout / add_registration_payment 皆受影響）。
  用 UniqueConstraint 讓 DB 層攔下第二筆。
  NULL 值在標準 SQL 允許重複（未帶 key 的歷史紀錄不會衝突）。

  極少數情況下若歷史資料已存在同 key 的兩筆（理論上的雙扣紀錄），
  先把較晚那筆的 key rename 為 `{key}_dup_{id}` 以解衝突，再建立 unique 約束。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "q5l6m7n8o9p0"
down_revision = "p4k5l6m7n8o9"
branch_labels = None
depends_on = None


_TABLE = "activity_payment_records"
_CONSTRAINT_NAME = "uq_activity_payment_records_idk"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    # 已存在同名 unique 約束則跳過
    for uq in inspector.get_unique_constraints(_TABLE):
        if uq.get("name") == _CONSTRAINT_NAME:
            return

    # 清理歷史重複（保留最早一筆，後續同 key 改 key 為 dup_ 後綴）
    op.execute(sa.text("""
            UPDATE activity_payment_records a
               SET idempotency_key = a.idempotency_key || '_dup_' || a.id
              FROM (
                  SELECT id,
                         idempotency_key,
                         ROW_NUMBER() OVER (
                             PARTITION BY idempotency_key
                             ORDER BY id ASC
                         ) AS rn
                    FROM activity_payment_records
                   WHERE idempotency_key IS NOT NULL
              ) dup
             WHERE a.id = dup.id
               AND dup.rn > 1
            """))

    op.create_unique_constraint(
        _CONSTRAINT_NAME,
        _TABLE,
        ["idempotency_key"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    for uq in inspector.get_unique_constraints(_TABLE):
        if uq.get("name") == _CONSTRAINT_NAME:
            op.drop_constraint(_CONSTRAINT_NAME, _TABLE, type_="unique")
            break
