"""employee amount columns: Float → Numeric(12, 2)

延續 20260419_j8e9f0a1b2c3：把 employees 表與 employee_contracts 表的金額欄位
也升級為 Numeric(12, 2)，徹底消除 salary 運算源頭的 float 尾數。

涵蓋：
- employees：base_salary / hourly_rate / supervisor_allowance / teacher_allowance /
  meal_allowance / transportation_allowance / other_allowance / insurance_salary_level
- employee_contracts：salary_at_contract

不改動：
- employees.pension_self_rate（0~0.06 比例，非金額）

Revision ID: k9f0a1b2c3d4
Revises: j8e9f0a1b2c3
Create Date: 2026-04-19
"""

from alembic import op
from sqlalchemy import inspect

revision = "k9f0a1b2c3d4"
down_revision = "j8e9f0a1b2c3"
branch_labels = None
depends_on = None


EMPLOYEE_MONEY_COLS = [
    "base_salary",
    "hourly_rate",
    "supervisor_allowance",
    "teacher_allowance",
    "meal_allowance",
    "transportation_allowance",
    "other_allowance",
    "insurance_salary_level",
]

CONTRACT_MONEY_COLS = ["salary_at_contract"]


def _is_postgres(bind) -> bool:
    return bind.dialect.name == "postgresql"


def _alter(table: str, columns: list[str], target_type: str, using_expr: str) -> None:
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
            f"ALTER COLUMN {col} TYPE {target_type} "
            f"USING {using_expr.format(col=col)}"
        )


def upgrade() -> None:
    _alter(
        "employees",
        EMPLOYEE_MONEY_COLS,
        "NUMERIC(12, 2)",
        "ROUND({col}::numeric, 2)",
    )
    _alter(
        "employee_contracts",
        CONTRACT_MONEY_COLS,
        "NUMERIC(12, 2)",
        "ROUND({col}::numeric, 2)",
    )


def downgrade() -> None:
    _alter(
        "employees",
        EMPLOYEE_MONEY_COLS,
        "DOUBLE PRECISION",
        "{col}::double precision",
    )
    _alter(
        "employee_contracts",
        CONTRACT_MONEY_COLS,
        "DOUBLE PRECISION",
        "{col}::double precision",
    )
