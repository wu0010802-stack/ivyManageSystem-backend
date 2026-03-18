"""add missing FK indexes (batch 2)

補齊 10 個 FK 欄位的索引，加速 JOIN 查詢：
- salary_records: bonus_config_id, attendance_policy_id
- employee_allowances: allowance_type_id
- class_bonus_settings: classroom_id
- grade_targets: bonus_config_id
- shift_assignments: shift_type_id
- daily_shifts: shift_type_id
- announcements: created_by
- announcement_reads: employee_id
- student_classroom_transfers: from_classroom_id

Revision ID: l8m9n0o1p2q3
Revises: k7l8m9n0o1p2
Create Date: 2026-03-18 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "l8m9n0o1p2q3"
down_revision = "k7l8m9n0o1p2"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    # salary_records: bonus_config_id
    if "salary_records" in tables:
        existing = _existing_indexes(bind, "salary_records")
        if "ix_salary_rec_bonus_config" not in existing:
            op.create_index(
                "ix_salary_rec_bonus_config",
                "salary_records",
                ["bonus_config_id"],
            )
        if "ix_salary_rec_attendance_policy" not in existing:
            op.create_index(
                "ix_salary_rec_attendance_policy",
                "salary_records",
                ["attendance_policy_id"],
            )

    # employee_allowances: allowance_type_id
    if "employee_allowances" in tables:
        existing = _existing_indexes(bind, "employee_allowances")
        if "ix_emp_allowance_type" not in existing:
            op.create_index(
                "ix_emp_allowance_type",
                "employee_allowances",
                ["allowance_type_id"],
            )

    # class_bonus_settings: classroom_id
    if "class_bonus_settings" in tables:
        existing = _existing_indexes(bind, "class_bonus_settings")
        if "ix_class_bonus_classroom" not in existing:
            op.create_index(
                "ix_class_bonus_classroom",
                "class_bonus_settings",
                ["classroom_id"],
            )

    # grade_targets: bonus_config_id
    if "grade_targets" in tables:
        existing = _existing_indexes(bind, "grade_targets")
        if "ix_grade_targets_bonus_config" not in existing:
            op.create_index(
                "ix_grade_targets_bonus_config",
                "grade_targets",
                ["bonus_config_id"],
            )

    # shift_assignments: shift_type_id
    if "shift_assignments" in tables:
        existing = _existing_indexes(bind, "shift_assignments")
        if "ix_shift_assign_shift_type" not in existing:
            op.create_index(
                "ix_shift_assign_shift_type",
                "shift_assignments",
                ["shift_type_id"],
            )

    # daily_shifts: shift_type_id
    if "daily_shifts" in tables:
        existing = _existing_indexes(bind, "daily_shifts")
        if "ix_daily_shift_shift_type" not in existing:
            op.create_index(
                "ix_daily_shift_shift_type",
                "daily_shifts",
                ["shift_type_id"],
            )

    # announcements: created_by
    if "announcements" in tables:
        existing = _existing_indexes(bind, "announcements")
        if "ix_announcements_created_by" not in existing:
            op.create_index(
                "ix_announcements_created_by",
                "announcements",
                ["created_by"],
            )

    # announcement_reads: employee_id
    # UniqueConstraint 前導是 announcement_id，需獨立單欄索引支援 WHERE employee_id = ?
    if "announcement_reads" in tables:
        existing = _existing_indexes(bind, "announcement_reads")
        if "ix_ann_reads_employee" not in existing:
            op.create_index(
                "ix_ann_reads_employee",
                "announcement_reads",
                ["employee_id"],
            )

    # student_classroom_transfers: from_classroom_id
    if "student_classroom_transfers" in tables:
        existing = _existing_indexes(bind, "student_classroom_transfers")
        if "ix_student_transfers_from_classroom" not in existing:
            op.create_index(
                "ix_student_transfers_from_classroom",
                "student_classroom_transfers",
                ["from_classroom_id"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if "salary_records" in tables:
        existing = _existing_indexes(bind, "salary_records")
        if "ix_salary_rec_bonus_config" in existing:
            op.drop_index("ix_salary_rec_bonus_config", table_name="salary_records")
        if "ix_salary_rec_attendance_policy" in existing:
            op.drop_index("ix_salary_rec_attendance_policy", table_name="salary_records")

    if "employee_allowances" in tables:
        existing = _existing_indexes(bind, "employee_allowances")
        if "ix_emp_allowance_type" in existing:
            op.drop_index("ix_emp_allowance_type", table_name="employee_allowances")

    if "class_bonus_settings" in tables:
        existing = _existing_indexes(bind, "class_bonus_settings")
        if "ix_class_bonus_classroom" in existing:
            op.drop_index("ix_class_bonus_classroom", table_name="class_bonus_settings")

    if "grade_targets" in tables:
        existing = _existing_indexes(bind, "grade_targets")
        if "ix_grade_targets_bonus_config" in existing:
            op.drop_index("ix_grade_targets_bonus_config", table_name="grade_targets")

    if "shift_assignments" in tables:
        existing = _existing_indexes(bind, "shift_assignments")
        if "ix_shift_assign_shift_type" in existing:
            op.drop_index("ix_shift_assign_shift_type", table_name="shift_assignments")

    if "daily_shifts" in tables:
        existing = _existing_indexes(bind, "daily_shifts")
        if "ix_daily_shift_shift_type" in existing:
            op.drop_index("ix_daily_shift_shift_type", table_name="daily_shifts")

    if "announcements" in tables:
        existing = _existing_indexes(bind, "announcements")
        if "ix_announcements_created_by" in existing:
            op.drop_index("ix_announcements_created_by", table_name="announcements")

    if "announcement_reads" in tables:
        existing = _existing_indexes(bind, "announcement_reads")
        if "ix_ann_reads_employee" in existing:
            op.drop_index("ix_ann_reads_employee", table_name="announcement_reads")

    if "student_classroom_transfers" in tables:
        existing = _existing_indexes(bind, "student_classroom_transfers")
        if "ix_student_transfers_from_classroom" in existing:
            op.drop_index("ix_student_transfers_from_classroom", table_name="student_classroom_transfers")
