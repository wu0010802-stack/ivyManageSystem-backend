"""year_end 子表 cycle FK：ON DELETE CASCADE → RESTRICT

Revision ID: cascfx01
Revises: moneyfx01
Create Date: 2026-06-12

Why（設計體檢 2026-06-12 Finding 4）:
    year_end 5 個子表（org_year_settings / class_enrollment_targets /
    employee_year_end_snapshot / year_end_settlements / special_bonus_items）
    對 year_end_cycles 的 FK 皆為 ON DELETE CASCADE——誤刪一列 cycle 即
    抹掉該年度全部結算/快照/特獎，無任何守衛。runtime 無刪 cycle 路徑、
    rebuild 走 upsert（settlement_builder idempotent），CASCADE 無正當使用者。

    改 RESTRICT：有子列時刪 cycle 由 DB 拒絕。ORM 同批拆掉
    YearEndCycle.settlements 的 delete-orphan cascade 並設 passive_deletes
    （models/year_end.py）。Employee.attendances/leaves/salaries 的同款
    ORM cascade 也一併移除——其 DB FK 本來就是 NO ACTION，不需 migration。

SQLite：無法 ALTER 既有 FK，略過（測試 DB 由 models metadata 直接建表，
已是 RESTRICT）。downgrade：還原 CASCADE。
"""

from alembic import op
from sqlalchemy import inspect

revision = "cascfx01"
down_revision = "moneyfx01"
branch_labels = None
depends_on = None


_CHILD_TABLES = [
    "org_year_settings",
    "class_enrollment_targets",
    "employee_year_end_snapshot",
    "year_end_settlements",
    "special_bonus_items",
]


def _find_cycle_fk_name(bind, table: str) -> str | None:
    """以 inspector 找該表指向 year_end_cycles(year_end_cycle_id) 的 FK 名稱，
    避免硬編 constraint 名在環境間漂移。"""
    for fk in inspect(bind).get_foreign_keys(table):
        if fk.get("referred_table") == "year_end_cycles" and fk.get(
            "constrained_columns"
        ) == ["year_end_cycle_id"]:
            return fk.get("name")
    return None


def _set_ondelete(ondelete: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    existing_tables = set(inspect(bind).get_table_names())
    for table in _CHILD_TABLES:
        if table not in existing_tables:
            continue
        fk_name = _find_cycle_fk_name(bind, table)
        if fk_name is None:
            continue
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.create_foreign_key(
            fk_name,
            table,
            "year_end_cycles",
            ["year_end_cycle_id"],
            ["id"],
            ondelete=ondelete,
        )


def upgrade() -> None:
    _set_ondelete("RESTRICT")


def downgrade() -> None:
    _set_ondelete("CASCADE")
