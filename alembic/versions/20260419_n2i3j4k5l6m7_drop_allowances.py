"""drop allowance tables and columns

Revision ID: n2i3j4k5l6m7
Revises: m1h2i3j4k5l6
Create Date: 2026-04-19

移除津貼邏輯：
- 刪除 employee_allowances 表（41 筆歷史資料）
- 刪除 allowance_types 表（6 筆類型資料）
- 刪除 employees 表的 5 個 allowance 欄位
- 刪除 salary_records 表的 5 個 allowance 欄位（28 筆歷史記錄的津貼數值將無法還原）
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "n2i3j4k5l6m7"
down_revision = "m1h2i3j4k5l6"
branch_labels = None
depends_on = None


ALLOWANCE_COLUMNS = [
    "supervisor_allowance",
    "teacher_allowance",
    "meal_allowance",
    "transportation_allowance",
    "other_allowance",
]


def _drop_columns_if_present(table: str, inspector) -> None:
    existing = {c["name"] for c in inspector.get_columns(table)}
    for col in ALLOWANCE_COLUMNS:
        if col in existing:
            op.drop_column(table, col)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "employee_allowances" in tables:
        op.drop_table("employee_allowances")
    if "allowance_types" in tables:
        op.drop_table("allowance_types")

    if "employees" in tables:
        _drop_columns_if_present("employees", inspector)
    if "salary_records" in tables:
        _drop_columns_if_present("salary_records", inspector)


def downgrade() -> None:
    # 不還原：降級恢復津貼邏輯需同時還原程式碼，
    # 單獨 downgrade DB 欄位會造成模型與 DB 不一致。若要還原，請用備份。
    raise NotImplementedError(
        "Downgrade not supported. Restore from backup to recover allowance data."
    )
