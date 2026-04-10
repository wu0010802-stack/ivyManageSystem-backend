"""add expected_start_label to recruitment_visits

新增 expected_start_label 欄位（預計就讀月份標籤），
讓統計查詢可改用 SQL GROUP BY 取代 Python 迴圈正則解析。

Revision ID: g7h8i9j0k1l2
Revises: f6g7h8i9j0k1
Create Date: 2026-04-10 00:00:00.000000
"""

from alembic import op
from sqlalchemy import inspect, text


revision = "g7h8i9j0k1l2"
down_revision = "f6g7h8i9j0k1"
branch_labels = None
depends_on = None

_TABLE = "recruitment_visits"
_COL   = "expected_start_label"
_IDX   = "ix_rv_expected_start_label"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COL not in cols:
        bind.execute(text(
            f"ALTER TABLE {_TABLE} ADD COLUMN {_COL} VARCHAR(30)"
        ))

    existing_idx = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    if _IDX not in existing_idx:
        op.create_index(_IDX, _TABLE, [_COL])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    existing_idx = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    if _IDX in existing_idx:
        op.drop_index(_IDX, table_name=_TABLE)

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COL in cols:
        bind.execute(text(f"ALTER TABLE {_TABLE} DROP COLUMN {_COL}"))
