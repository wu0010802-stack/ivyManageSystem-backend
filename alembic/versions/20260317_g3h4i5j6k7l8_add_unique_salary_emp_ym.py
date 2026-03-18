"""add unique constraint on salary_records(employee_id, salary_year, salary_month)

同員工同月只允許一筆薪資記錄，防止重複計算產生不確定性查詢結果。

Revision ID: g3h4i5j6k7l8
Revises: f2a3b4c5d6e7
Create Date: 2026-03-17 00:02:00.000000
"""

from alembic import op
from sqlalchemy import inspect, text


revision = "g3h4i5j6k7l8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # 1. 清除重複記錄：同 employee_id/year/month 只保留 id 最大的一筆
    bind.execute(text(
        """
        DELETE FROM salary_records
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM salary_records
            GROUP BY employee_id, salary_year, salary_month
        )
        """
    ))

    # 2. 移除舊的普通索引（若存在）
    indexes = {idx["name"] for idx in inspector.get_indexes("salary_records")}
    if "ix_salary_emp_ym" in indexes:
        op.drop_index("ix_salary_emp_ym", table_name="salary_records")

    # 3. 建立唯一約束（同時建立唯一索引）
    constraints = {c["name"] for c in inspector.get_unique_constraints("salary_records")}
    if "uq_salary_emp_ym" not in constraints:
        op.create_unique_constraint(
            "uq_salary_emp_ym",
            "salary_records",
            ["employee_id", "salary_year", "salary_month"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # 移除唯一約束
    constraints = {c["name"] for c in inspector.get_unique_constraints("salary_records")}
    if "uq_salary_emp_ym" in constraints:
        op.drop_constraint("uq_salary_emp_ym", "salary_records", type_="unique")

    # 還原為普通索引
    indexes = {idx["name"] for idx in inspector.get_indexes("salary_records")}
    if "ix_salary_emp_ym" not in indexes:
        op.create_index(
            "ix_salary_emp_ym",
            "salary_records",
            ["employee_id", "salary_year", "salary_month"],
        )
