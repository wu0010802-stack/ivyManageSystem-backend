"""add recruitment performance indexes

新增 recruitment_visits 常用查詢欄位的索引，改善統計查詢效能。

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2026-04-10 00:00:00.000000
"""

from alembic import op
from sqlalchemy import inspect


revision = "f6g7h8i9j0k1"
down_revision = "e5f6g7h8i9j0"
branch_labels = None
depends_on = None


_INDEX_DEFS = [
    ("ix_rv_has_deposit",        ["has_deposit"]),
    ("ix_rv_no_deposit_reason",  ["no_deposit_reason"]),
    ("ix_rv_has_deposit_grade",  ["has_deposit", "grade"]),
    ("ix_rv_source_grade",       ["source", "grade"]),
    ("ix_rv_referrer_grade",     ["referrer", "grade"]),
    ("ix_rv_month_has_deposit",  ["month", "has_deposit"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "recruitment_visits" not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes("recruitment_visits")}
    for name, cols in _INDEX_DEFS:
        if name not in existing:
            op.create_index(name, "recruitment_visits", cols)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "recruitment_visits" not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes("recruitment_visits")}
    for name, _cols in _INDEX_DEFS:
        if name in existing:
            op.drop_index(name, table_name="recruitment_visits")
