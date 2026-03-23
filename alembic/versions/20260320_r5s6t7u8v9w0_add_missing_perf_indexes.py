"""add missing performance indexes

補齊尚未建立的高頻查詢索引：
- salary_records: (salary_year, salary_month, is_finalized) — 快速判斷整月是否已封存
- leave_records: (employee_id, leave_type, is_approved) — 假別配額計算
- attendances: (attendance_date, is_late, is_early_leave, ...) — 異常查詢複合索引

Revision ID: r5s6t7u8v9w0
Revises: q4r5s6t7u8v9
Create Date: 2026-03-20 00:00:00.000000
"""

from alembic import op
from sqlalchemy import inspect

revision = "r5s6t7u8v9w0"
down_revision = "q4r5s6t7u8v9"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    # salary_records: (salary_year, salary_month, is_finalized)
    # 現有 ix_salary_emp_ym_finalized 以 employee_id 為前導，無 employee_id 條件的封存月份判斷走不到
    if "salary_records" in tables:
        existing = _existing_indexes(bind, "salary_records")
        if "ix_salary_ym_finalized" not in existing:
            op.create_index(
                "ix_salary_ym_finalized",
                "salary_records",
                ["salary_year", "salary_month", "is_finalized"],
            )

    # leave_records: (employee_id, leave_type, is_approved)
    # 假別配額計算：WHERE employee_id=? AND leave_type=? AND is_approved=True
    if "leave_records" in tables:
        existing = _existing_indexes(bind, "leave_records")
        if "ix_leave_emp_type_approved" not in existing:
            op.create_index(
                "ix_leave_emp_type_approved",
                "leave_records",
                ["employee_id", "leave_type", "is_approved"],
            )

    # attendances: (attendance_date, is_late, is_early_leave, is_missing_punch_in, is_missing_punch_out)
    # 考勤異常彙整頁：WHERE date BETWEEN ? AND ? AND (is_late OR is_early_leave OR ...)
    if "attendances" in tables:
        existing = _existing_indexes(bind, "attendances")
        if "ix_attendance_anomaly" not in existing:
            op.create_index(
                "ix_attendance_anomaly",
                "attendances",
                [
                    "attendance_date",
                    "is_late",
                    "is_early_leave",
                    "is_missing_punch_in",
                    "is_missing_punch_out",
                ],
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if "salary_records" in tables:
        existing = _existing_indexes(bind, "salary_records")
        if "ix_salary_ym_finalized" in existing:
            op.drop_index("ix_salary_ym_finalized", table_name="salary_records")

    if "leave_records" in tables:
        existing = _existing_indexes(bind, "leave_records")
        if "ix_leave_emp_type_approved" in existing:
            op.drop_index("ix_leave_emp_type_approved", table_name="leave_records")

    if "attendances" in tables:
        existing = _existing_indexes(bind, "attendances")
        if "ix_attendance_anomaly" in existing:
            op.drop_index("ix_attendance_anomaly", table_name="attendances")
