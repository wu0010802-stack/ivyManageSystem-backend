"""gov_data_sync 三 FK 補 ondelete=SET NULL

延續 2026-05-12 bug sweep。原 `20260507_05df4844e040_gov_data_sync.py` 三處 FK
指向 `gov_data_snapshots.id` 但無 `ondelete=` 明示，PG 預設 NO ACTION：
snapshot 屬可清理稽核資料，無 ondelete 會擋掉 `minimum_wage_history` /
`minimum_wage_staging` / `insurance_brackets` 的刪除，且呼叫端難以察覺。
統一改 SET NULL，snapshot 清理時這三表 source_snapshot_id 自動斷鏈、不丟資料。

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-05-12

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


_TARGETS = [
    # (table, column, fk_name)
    (
        "minimum_wage_history",
        "source_snapshot_id",
        "fk_minimum_wage_history_source_snapshot",
    ),
    (
        "minimum_wage_staging",
        "source_snapshot_id",
        "fk_minimum_wage_staging_source_snapshot",
    ),
    (
        "insurance_brackets",
        "source_snapshot_id",
        "fk_insurance_brackets_source_snapshot",
    ),
]


def _find_existing_fk(bind, table: str, column: str) -> str | None:
    """回傳指向 gov_data_snapshots(id) 且包含此欄位的 FK 名稱；找不到回 None。"""
    insp = inspect(bind)
    if table not in insp.get_table_names():
        return None
    for fk in insp.get_foreign_keys(table):
        if fk.get("referred_table") == "gov_data_snapshots" and column in (
            fk.get("constrained_columns") or []
        ):
            return fk.get("name")
    return None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # SQLite 不支援 ALTER TABLE DROP CONSTRAINT；本檔僅在 PG 執行。
    # （測試用 SQLite in-memory，不依賴 FK ondelete 行為）
    if dialect != "postgresql":
        return

    for table, column, new_name in _TARGETS:
        existing = _find_existing_fk(bind, table, column)
        if existing is None:
            continue  # 原 FK 不存在（測試環境或已手動清理），跳過
        op.drop_constraint(existing, table, type_="foreignkey")
        op.create_foreign_key(
            new_name,
            table,
            "gov_data_snapshots",
            [column],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect != "postgresql":
        return

    for table, column, new_name in _TARGETS:
        # 還原為 NO ACTION（原始狀態）。先 drop SET NULL FK，再加回不帶 ondelete 的 FK。
        existing = _find_existing_fk(bind, table, column)
        if existing is None:
            continue
        op.drop_constraint(existing, table, type_="foreignkey")
        # 還原名稱用 alembic autogenerate 慣例
        op.create_foreign_key(
            None,
            table,
            "gov_data_snapshots",
            [column],
            ["id"],
        )
