"""salary amount columns: Float(double precision) → Numeric(12, 2)

精度升級：避免 double precision 浮點累積誤差導致薪資對帳尾數失真
（例如 1234.56 儲存為 1234.5599999999999）。改為 Numeric(12, 2) 後
儲存精度精確到小數 2 位，上限 9,999,999,999.99。

涵蓋三張表：
- salary_records：所有金額欄位（base_salary、allowances、bonuses、insurances、deductions、gross/total/net）
- employee_allowances.amount
- salary_items.amount / unit_amount

不改動欄位：
- salary_records.work_hours（天然小數工時，非金額）
- Employee.base_salary / hourly_rate / allowances（本次不動員工合約欄位，改動成本大且非累積運算源頭）
- InsuranceTable.*（靜態參考表，政府公告金額本身為整數）

PG：使用 ALTER COLUMN TYPE NUMERIC(12, 2) USING col::numeric(12,2)
SQLite：Float 與 Numeric 底層都是 REAL，no-op（inspector 偵測不到差異）

Revision ID: j8e9f0a1b2c3
Revises: i7d8e9f0a1b2
Create Date: 2026-04-19
"""

from alembic import op
from sqlalchemy import inspect

revision = "j8e9f0a1b2c3"
down_revision = "i7d8e9f0a1b2"
branch_labels = None
depends_on = None


SALARY_MONEY_COLS = [
    "base_salary",
    "supervisor_allowance",
    "teacher_allowance",
    "meal_allowance",
    "transportation_allowance",
    "other_allowance",
    "festival_bonus",
    "overtime_bonus",
    "performance_bonus",
    "special_bonus",
    "overtime_pay",
    "meeting_overtime_pay",
    "meeting_absence_deduction",
    "birthday_bonus",
    "hourly_rate",
    "hourly_total",
    "labor_insurance_employee",
    "labor_insurance_employer",
    "health_insurance_employee",
    "health_insurance_employer",
    "pension_employee",
    "pension_employer",
    "late_deduction",
    "early_leave_deduction",
    "missing_punch_deduction",
    "leave_deduction",
    "absence_deduction",
    "other_deduction",
    "gross_salary",
    "total_deduction",
    "net_salary",
    "bonus_amount",
    "supervisor_dividend",
]


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
    _alter_to_numeric("salary_records", SALARY_MONEY_COLS)
    _alter_to_numeric("employee_allowances", ["amount"])
    _alter_to_numeric("salary_items", ["amount", "unit_amount"])


def downgrade() -> None:
    _alter_to_float("salary_records", SALARY_MONEY_COLS)
    _alter_to_float("employee_allowances", ["amount"])
    _alter_to_float("salary_items", ["amount", "unit_amount"])
