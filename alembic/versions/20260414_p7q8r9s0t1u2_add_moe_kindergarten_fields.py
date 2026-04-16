"""add moe kindergarten fields to competitor_school

新增教育部幼兒園資料欄位（核准設立日期、全園總面積）至 competitor_school 表，
並新增 source_key 欄位（若尚未存在）以支援 MOE ECE 資料同步。

Revision ID: p7q8r9s0t1u2
Revises: o6p7q8r9s0t1
Create Date: 2026-04-14 00:00:00.000000
"""

from alembic import op
from sqlalchemy import inspect, text


revision = "p7q8r9s0t1u2"
down_revision = "o6p7q8r9s0t1"
branch_labels = None
depends_on = None

_TABLE = "competitor_school"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if _TABLE not in inspector.get_table_names():
        return

    existing_cols = {col["name"] for col in inspector.get_columns(_TABLE)}

    if "approved_date" not in existing_cols:
        bind.execute(text(f"ALTER TABLE {_TABLE} ADD COLUMN approved_date VARCHAR(20)"))

    if "total_area_sqm" not in existing_cols:
        bind.execute(text(f"ALTER TABLE {_TABLE} ADD COLUMN total_area_sqm DOUBLE PRECISION"))

    # source_key 在某些舊環境可能不存在（由舊 migration raw SQL 建立）
    if "source_key" not in existing_cols:
        bind.execute(text(f"ALTER TABLE {_TABLE} ADD COLUMN source_key VARCHAR(120)"))
        bind.execute(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS ix_competitor_school_source_key "
            f"ON {_TABLE} (source_key)"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if _TABLE not in inspector.get_table_names():
        return

    existing_cols = {col["name"] for col in inspector.get_columns(_TABLE)}

    if "total_area_sqm" in existing_cols:
        bind.execute(text(f"ALTER TABLE {_TABLE} DROP COLUMN total_area_sqm"))

    if "approved_date" in existing_cols:
        bind.execute(text(f"ALTER TABLE {_TABLE} DROP COLUMN approved_date"))
