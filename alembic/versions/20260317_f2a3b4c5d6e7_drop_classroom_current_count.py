"""drop classrooms.current_count column

current_count 從未被維護（轉班/刪除學生後不更新），
API 回傳的 current_count 已全面改用即時查詢（COUNT + is_active=True），
保留此欄只會造成誤導，故移除。

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-03-17 00:01:00.000000
"""

from alembic import op
from sqlalchemy import inspect


revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = [c["name"] for c in inspector.get_columns("classrooms")]
    if "current_count" in cols:
        op.drop_column("classrooms", "current_count")


def downgrade() -> None:
    import sqlalchemy as sa
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = [c["name"] for c in inspector.get_columns("classrooms")]
    if "current_count" not in cols:
        op.add_column("classrooms", sa.Column("current_count", sa.Integer(), nullable=True))
