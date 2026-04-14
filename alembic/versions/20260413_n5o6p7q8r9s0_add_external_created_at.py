"""add external_created_at to recruitment_visits

儲存義華官網後台「資料建立時間」原始字串，修正前端顯示為 DB 匯入時間的問題。

Revision ID: n5o6p7q8r9s0
Revises: m3n4o5p6q7r8
Create Date: 2026-04-13 00:00:00.000000
"""

from alembic import op
from sqlalchemy import inspect, text

revision = "n5o6p7q8r9s0"
down_revision = "m3n4o5p6q7r8"
branch_labels = None
depends_on = None

_TABLE = "recruitment_visits"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns(_TABLE)}
    if "external_created_at" not in columns:
        bind.execute(text(f"ALTER TABLE {_TABLE} ADD COLUMN external_created_at VARCHAR(50)"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns(_TABLE)}
    if "external_created_at" in columns:
        bind.execute(text(f"ALTER TABLE {_TABLE} DROP COLUMN external_created_at"))
