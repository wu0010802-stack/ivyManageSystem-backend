"""殘存 Float 金額欄 → Numeric(12, 2)（第二波，補 j8e9f0a1b2c3 未涵蓋的表）

Revision ID: moneyfx01
Revises: auditack02
Create Date: 2026-06-12

Why（設計體檢 2026-06-12 Finding 3）:
    j8e9f0a1b2c3 只轉了 salary_records / employee_allowances / salary_items，
    以下金額欄仍為 Float（double precision），浮點誤差會原樣 persist：
    - overtime_records.overtime_pay、meeting_records.overtime_pay
      （兩條已證實流入 gross_salary 的累加路徑）
    - bonus_settings.calculated_festival_bonus / calculated_overtime_bonus
    - position_salary_configs 15 個標準底薪欄
    - bonus_configs 主管紅利 4 欄（principal/director/leader/vice_leader_dividend）
    - insurance_tables 金額欄（salary_min/max、insured_amount、各負擔額）

    ORM 同批改用 models/types.Money（讀出轉 float，Python 計算層不變）。
    率欄位（insurance_tables.labor_rate_employee 等 5 欄、bonus_configs 其餘
    獎金基數以外的 threshold 率欄）非金額，維持 Float 不動。

PG：ALTER COLUMN TYPE NUMERIC(12, 2) USING ROUND(col::numeric, 2)
SQLite：Float 與 Numeric 底層都是 REAL，no-op（照 j8e9f0a1b2c3 慣例）。
downgrade：還原 DOUBLE PRECISION。
"""

from alembic import op
from sqlalchemy import inspect

revision = "moneyfx01"
down_revision = "auditack02"
branch_labels = None
depends_on = None


# table → 金額欄位清單
_MONEY_COLS: dict[str, list[str]] = {
    "overtime_records": ["overtime_pay"],
    "meeting_records": ["overtime_pay"],
    "bonus_settings": ["calculated_festival_bonus", "calculated_overtime_bonus"],
    "position_salary_configs": [
        "head_teacher_a",
        "head_teacher_b",
        "head_teacher_c",
        "assistant_teacher_a",
        "assistant_teacher_b",
        "assistant_teacher_c",
        "admin_staff",
        "english_teacher",
        "art_teacher",
        "designer",
        "nurse",
        "driver",
        "kitchen_staff",
        "director",
        "principal",
    ],
    "bonus_configs": [
        "principal_dividend",
        "director_dividend",
        "leader_dividend",
        "vice_leader_dividend",
    ],
    "insurance_tables": [
        "salary_min",
        "salary_max",
        "insured_amount",
        "labor_employee",
        "labor_employer",
        "health_employee",
        "health_employer",
        "pension_employer_amount",
    ],
}


def _is_postgres(bind) -> bool:
    return bind.dialect.name == "postgresql"


def _alter_to_numeric(table: str, columns: list[str]) -> None:
    """PG only：把指定欄位改為 Numeric(12, 2)。SQLite 下略過。"""
    bind = op.get_bind()
    if not _is_postgres(bind):
        return
    if table not in inspect(bind).get_table_names():
        return
    existing = {c["name"] for c in inspect(bind).get_columns(table)}
    for col in columns:
        if col not in existing:
            continue
        op.execute(
            f"ALTER TABLE {table} "
            f"ALTER COLUMN {col} TYPE NUMERIC(12, 2) "
            f"USING ROUND({col}::numeric, 2)"
        )


def _alter_to_float(table: str, columns: list[str]) -> None:
    """downgrade：Numeric(12, 2) → double precision。"""
    bind = op.get_bind()
    if not _is_postgres(bind):
        return
    if table not in inspect(bind).get_table_names():
        return
    existing = {c["name"] for c in inspect(bind).get_columns(table)}
    for col in columns:
        if col not in existing:
            continue
        op.execute(
            f"ALTER TABLE {table} "
            f"ALTER COLUMN {col} TYPE DOUBLE PRECISION "
            f"USING {col}::double precision"
        )


def upgrade() -> None:
    for table, columns in _MONEY_COLS.items():
        _alter_to_numeric(table, columns)


def downgrade() -> None:
    for table, columns in _MONEY_COLS.items():
        _alter_to_float(table, columns)
