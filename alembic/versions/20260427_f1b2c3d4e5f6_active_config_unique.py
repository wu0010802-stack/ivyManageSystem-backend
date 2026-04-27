"""enforce single-active for AttendancePolicy/BonusConfig/InsuranceRate

修正薪資設定鏈路問題：歷史資料或併發更新可能在
attendance_policies / bonus_configs / insurance_rates 留下多筆 is_active=true，
導致薪資引擎隨機載入舊版設定，且 SalaryRecord 會記錄錯誤 config id。

本遷移：
  1) 對每張表，保留 id 最大的一筆 active，把較舊的 active 全部 deactivate（資料修補）。
  2) 在 PostgreSQL 上建立 partial unique index，保證日後同時間只有一筆 active。
     SQLite 不建立此索引（測試環境本身只插入一筆 active，不需此守衛）。

Revision ID: f1b2c3d4e5f6
Revises: e0a1b2c3d4e5
Create Date: 2026-04-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "f1b2c3d4e5f6"
down_revision = "e0a1b2c3d4e5"
branch_labels = None
depends_on = None


_TABLES = ("attendance_policies", "bonus_configs", "insurance_rates")
_INDEX_NAMES = {
    "attendance_policies": "uq_attendance_policies_active",
    "bonus_configs": "uq_bonus_configs_active",
    "insurance_rates": "uq_insurance_rates_active",
}


def _index_names(bind, table: str) -> set:
    if table not in inspect(bind).get_table_names():
        return set()
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def _dedupe_active(bind, table: str) -> None:
    """保留 id 最大的 active，較舊的全部 deactivate。"""
    if table not in inspect(bind).get_table_names():
        return
    bind.execute(sa.text(f"""
            UPDATE {table} SET is_active = FALSE
            WHERE is_active = TRUE
              AND id NOT IN (SELECT MAX(id) FROM {table} WHERE is_active = TRUE)
            """))


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    for table in _TABLES:
        _dedupe_active(bind, table)

    if not is_pg:
        return

    for table in _TABLES:
        idx_name = _INDEX_NAMES[table]
        if idx_name not in _index_names(bind, table):
            op.execute(sa.text(f"""
                    CREATE UNIQUE INDEX {idx_name}
                    ON {table} (is_active)
                    WHERE is_active = TRUE
                    """))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in _TABLES:
        idx_name = _INDEX_NAMES[table]
        if idx_name in _index_names(bind, table):
            op.drop_index(idx_name, table_name=table)
