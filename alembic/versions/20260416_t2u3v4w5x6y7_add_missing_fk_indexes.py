"""add missing FK indexes

補齊 22 個 Foreign Key 欄位缺失的索引。
FK 欄位無索引會導致：
1. JOIN 查詢走 sequential scan（效能差）
2. 父表 DELETE 時需全表掃描子表確認無參照（鎖表風險）

Revision ID: t2u3v4w5x6y7
Revises: s0t1u2v3w4x5
Create Date: 2026-04-16 00:00:00.000000
"""

from alembic import op
from sqlalchemy import inspect

revision = "t2u3v4w5x6y7"
down_revision = "s0t1u2v3w4x5"
branch_labels = None
depends_on = None

# (index_name, table, columns)
_FK_INDEXES = [
    # 高優先：薪資/考勤批次計算直接使用
    ("ix_fk_attendance_employee", "attendances", ["employee_id"]),
    ("ix_fk_meeting_employee", "meeting_records", ["employee_id"]),
    ("ix_fk_overtime_employee", "overtime_records", ["employee_id"]),
    ("ix_fk_emp_allowance_employee", "employee_allowances", ["employee_id"]),
    # 中優先：班級查詢常用
    ("ix_fk_classroom_head_teacher", "classrooms", ["head_teacher_id"]),
    ("ix_fk_classroom_assistant", "classrooms", ["assistant_teacher_id"]),
    ("ix_fk_classroom_art_teacher", "classrooms", ["art_teacher_id"]),
    ("ix_fk_classroom_grade", "classrooms", ["grade_id"]),
    ("ix_fk_student_classroom", "students", ["classroom_id"]),
    # 低優先：低頻操作但仍應有索引
    ("ix_fk_stu_changelog_recorded", "student_change_logs", ["recorded_by"]),
    ("ix_fk_stu_changelog_classroom", "student_change_logs", ["classroom_id"]),
    ("ix_fk_stu_changelog_from", "student_change_logs", ["from_classroom_id"]),
    ("ix_fk_stu_changelog_to", "student_change_logs", ["to_classroom_id"]),
    (
        "ix_fk_dismissal_completed",
        "student_dismissal_calls",
        ["completed_by_employee_id"],
    ),
    ("ix_fk_dismissal_requested", "student_dismissal_calls", ["requested_by_user_id"]),
    (
        "ix_fk_dismissal_acked",
        "student_dismissal_calls",
        ["acknowledged_by_employee_id"],
    ),
    ("ix_fk_assessment_recorded", "student_assessments", ["recorded_by"]),
    ("ix_fk_stu_att_recorded", "student_attendances", ["recorded_by"]),
    ("ix_fk_incident_recorded", "student_incidents", ["recorded_by"]),
    ("ix_fk_transfer_by", "student_classroom_transfers", ["transferred_by"]),
    ("ix_fk_swap_req_shift", "shift_swap_requests", ["requester_shift_type_id"]),
    ("ix_fk_swap_tgt_shift", "shift_swap_requests", ["target_shift_type_id"]),
]


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    for idx_name, table, columns in _FK_INDEXES:
        if table not in tables:
            continue
        existing = _existing_indexes(bind, table)
        if idx_name in existing:
            continue
        op.create_index(idx_name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)
    for idx_name, table, _columns in _FK_INDEXES:
        if table not in tables:
            continue
        existing = _existing_indexes(bind, table)
        if idx_name in existing:
            op.drop_index(idx_name, table_name=table)
