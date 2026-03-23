"""add query optimization indexes

補齊薪資批次計算與請假查詢的高頻索引：
- employees: (is_active, resign_date) — 薪資批次計算 OR 條件含離職當月員工
- meeting_records: (meeting_date, attended) — 薪資計算節慶缺席查詢
- users: (employee_id, is_active) — 請假/加班列表批次查角色
- registration_courses: (registration_id, course_id, status) — 改善 JOIN 效能（補充現有 ix_reg_courses_status）

Revision ID: n1o2p3q4r5s6
Revises: m9n0o1p2q3r4
Create Date: 2026-03-19 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "n1o2p3q4r5s6"
down_revision = "m9n0o1p2q3r4"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    # employees: (is_active, resign_date)
    # 薪資批次計算：OR(is_active=True, resign_date BETWEEN month_start AND month_end)
    if "employees" in tables:
        existing = _existing_indexes(bind, "employees")
        if "ix_employee_active_resign" not in existing:
            op.create_index(
                "ix_employee_active_resign",
                "employees",
                ["is_active", "resign_date"],
            )

    # meeting_records: (meeting_date, attended)
    # 薪資計算中節慶缺席查詢：WHERE meeting_date BETWEEN ? AND ? AND attended=False
    if "meeting_records" in tables:
        existing = _existing_indexes(bind, "meeting_records")
        if "ix_meeting_date_attended" not in existing:
            op.create_index(
                "ix_meeting_date_attended",
                "meeting_records",
                ["meeting_date", "attended"],
            )

    # users: (employee_id, is_active)
    # 請假/加班列表批次查詢員工角色：WHERE employee_id IN (...) AND is_active=True
    if "users" in tables:
        existing = _existing_indexes(bind, "users")
        if "ix_user_emp_active" not in existing:
            op.create_index(
                "ix_user_emp_active",
                "users",
                ["employee_id", "is_active"],
            )

    # registration_courses: (registration_id, course_id, status)
    # 補充現有 ix_reg_courses_status(course_id, status)，改善按 registration_id 過濾的 JOIN
    if "registration_courses" in tables:
        existing = _existing_indexes(bind, "registration_courses")
        if "ix_reg_course_reg_status" not in existing:
            op.create_index(
                "ix_reg_course_reg_status",
                "registration_courses",
                ["registration_id", "course_id", "status"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if "employees" in tables:
        existing = _existing_indexes(bind, "employees")
        if "ix_employee_active_resign" in existing:
            op.drop_index("ix_employee_active_resign", table_name="employees")

    if "meeting_records" in tables:
        existing = _existing_indexes(bind, "meeting_records")
        if "ix_meeting_date_attended" in existing:
            op.drop_index("ix_meeting_date_attended", table_name="meeting_records")

    if "users" in tables:
        existing = _existing_indexes(bind, "users")
        if "ix_user_emp_active" in existing:
            op.drop_index("ix_user_emp_active", table_name="users")

    if "registration_courses" in tables:
        existing = _existing_indexes(bind, "registration_courses")
        if "ix_reg_course_reg_status" in existing:
            op.drop_index("ix_reg_course_reg_status", table_name="registration_courses")
