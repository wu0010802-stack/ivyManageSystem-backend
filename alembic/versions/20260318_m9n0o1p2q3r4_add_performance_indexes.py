"""add performance indexes for common query patterns

補齊高頻查詢缺少的索引：
- salary_records: (salary_year, salary_month) — 查詢整月薪資清單
- overtime_records: (is_approved, overtime_date) — 審核狀態過濾 + 日期範圍
- attendances: (attendance_date) — 今日異常、月份統計（無 employee_id 前綴的查詢）
- punch_correction_requests: (is_approved, attendance_date) — 待審補打卡查詢
- leave_records: (employee_id, leave_type, start_date) — 配額計算複合查詢
- school_events: (event_date) — 按日期查詢行事曆

Revision ID: m9n0o1p2q3r4
Revises: l8m9n0o1p2q3
Create Date: 2026-03-18 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "m9n0o1p2q3r4"
down_revision = "l8m9n0o1p2q3"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    # salary_records: (salary_year, salary_month)
    # 現有 UniqueConstraint 前導是 employee_id，無 employee_id 條件的月份查詢走不到
    if "salary_records" in tables:
        existing = _existing_indexes(bind, "salary_records")
        if "ix_salary_ym" not in existing:
            op.create_index(
                "ix_salary_ym",
                "salary_records",
                ["salary_year", "salary_month"],
            )

    # overtime_records: (is_approved, overtime_date)
    # 管理端按審核狀態列表 + 薪資計算批次查詢
    if "overtime_records" in tables:
        existing = _existing_indexes(bind, "overtime_records")
        if "ix_overtime_approval_date" not in existing:
            op.create_index(
                "ix_overtime_approval_date",
                "overtime_records",
                ["is_approved", "overtime_date"],
            )

    # attendances: (attendance_date)
    # 今日異常查詢與月份統計（不含 employee_id 前綴）
    if "attendances" in tables:
        existing = _existing_indexes(bind, "attendances")
        if "ix_attendance_date" not in existing:
            op.create_index(
                "ix_attendance_date",
                "attendances",
                ["attendance_date"],
            )

    # punch_correction_requests: (is_approved, attendance_date)
    # 管理端按審核狀態過濾補打卡申請
    if "punch_correction_requests" in tables:
        existing = _existing_indexes(bind, "punch_correction_requests")
        if "ix_punch_correction_approval" not in existing:
            op.create_index(
                "ix_punch_correction_approval",
                "punch_correction_requests",
                ["is_approved", "attendance_date"],
            )

    # leave_records: (employee_id, leave_type, start_date)
    # 配額計算：WHERE employee_id=? AND leave_type=? AND is_approved=? AND start_date BETWEEN ?
    # 現有 ix_leave_emp_dates 為 (employee_id, start_date, end_date)，不含 leave_type
    if "leave_records" in tables:
        existing = _existing_indexes(bind, "leave_records")
        if "ix_leave_emp_type_date" not in existing:
            op.create_index(
                "ix_leave_emp_type_date",
                "leave_records",
                ["employee_id", "leave_type", "start_date"],
            )

    # school_events: (event_date)
    # 按日期查詢行事曆事件
    if "school_events" in tables:
        existing = _existing_indexes(bind, "school_events")
        if "ix_school_event_date" not in existing:
            op.create_index(
                "ix_school_event_date",
                "school_events",
                ["event_date"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if "salary_records" in tables:
        existing = _existing_indexes(bind, "salary_records")
        if "ix_salary_ym" in existing:
            op.drop_index("ix_salary_ym", table_name="salary_records")

    if "overtime_records" in tables:
        existing = _existing_indexes(bind, "overtime_records")
        if "ix_overtime_approval_date" in existing:
            op.drop_index("ix_overtime_approval_date", table_name="overtime_records")

    if "attendances" in tables:
        existing = _existing_indexes(bind, "attendances")
        if "ix_attendance_date" in existing:
            op.drop_index("ix_attendance_date", table_name="attendances")

    if "punch_correction_requests" in tables:
        existing = _existing_indexes(bind, "punch_correction_requests")
        if "ix_punch_correction_approval" in existing:
            op.drop_index("ix_punch_correction_approval", table_name="punch_correction_requests")

    if "leave_records" in tables:
        existing = _existing_indexes(bind, "leave_records")
        if "ix_leave_emp_type_date" in existing:
            op.drop_index("ix_leave_emp_type_date", table_name="leave_records")

    if "school_events" in tables:
        existing = _existing_indexes(bind, "school_events")
        if "ix_school_event_date" in existing:
            op.drop_index("ix_school_event_date", table_name="school_events")
