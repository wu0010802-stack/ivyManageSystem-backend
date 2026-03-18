"""add missing FK indexes for performance

補齊 salary_items, employees, leave_records, approval_logs 上缺少的 FK 索引，
避免 JOIN / WHERE 觸發全表掃描。

Revision ID: i5j6k7l8m9n0
Revises: h4i5j6k7l8m9
Create Date: 2026-03-17 00:10:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "i5j6k7l8m9n0"
down_revision = "h4i5j6k7l8m9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    def _existing_indexes(table: str) -> set[str]:
        return {idx["name"] for idx in inspector.get_indexes(table)}

    # salary_items.salary_record_id
    if "ix_salary_items_record" not in _existing_indexes("salary_items"):
        op.create_index("ix_salary_items_record", "salary_items", ["salary_record_id"])

    # employees.job_title_id
    if "ix_employees_job_title" not in _existing_indexes("employees"):
        op.create_index("ix_employees_job_title", "employees", ["job_title_id"])

    # employees.classroom_id
    if "ix_employees_classroom" not in _existing_indexes("employees"):
        op.create_index("ix_employees_classroom", "employees", ["classroom_id"])

    # leave_records.substitute_employee_id
    if "ix_leave_substitute" not in _existing_indexes("leave_records"):
        op.create_index("ix_leave_substitute", "leave_records", ["substitute_employee_id"])

    # approval_logs.approver_id
    if "ix_approval_log_approver" not in _existing_indexes("approval_logs"):
        op.create_index("ix_approval_log_approver", "approval_logs", ["approver_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    def _existing_indexes(table: str) -> set[str]:
        return {idx["name"] for idx in inspector.get_indexes(table)}

    if "ix_salary_items_record" in _existing_indexes("salary_items"):
        op.drop_index("ix_salary_items_record", table_name="salary_items")

    if "ix_employees_job_title" in _existing_indexes("employees"):
        op.drop_index("ix_employees_job_title", table_name="employees")

    if "ix_employees_classroom" in _existing_indexes("employees"):
        op.drop_index("ix_employees_classroom", table_name="employees")

    if "ix_leave_substitute" in _existing_indexes("leave_records"):
        op.drop_index("ix_leave_substitute", table_name="leave_records")

    if "ix_approval_log_approver" in _existing_indexes("approval_logs"):
        op.drop_index("ix_approval_log_approver", table_name="approval_logs")
