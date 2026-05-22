"""employee leave attendance sync

Revision ID: empleavesync
Revises: recurr01
Create Date: 2026-05-22

注意：down_revision 對應 worktree 內 head（recurr01）。
merge to main 前 user 自行 rebase 到 main 最新 head。
# TODO: rebase down_revision before merging into main
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "empleavesync"
down_revision = "recurr01"  # TODO: rebase before merge — worktree head at 2026-05-22
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "attendances",
        sa.Column("leave_record_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "attendances",
        sa.Column("partial_leave_hours", sa.Numeric(4, 2), nullable=True),
    )
    op.create_foreign_key(
        "fk_attendance_leave",
        "attendances",
        "leave_records",
        ["leave_record_id"],
        ["id"],
        ondelete="SET NULL",
    )

    conn = op.get_bind()
    dups = conn.execute(text("""
            SELECT employee_id, attendance_date, COUNT(*) c
            FROM attendances
            GROUP BY employee_id, attendance_date HAVING COUNT(*) > 1
        """)).fetchall()
    if dups:
        raise RuntimeError(
            f"偵測到 {len(dups)} 組 (employee_id, attendance_date) 重複，"
            f"請先跑 scripts/dedupe_attendance.py 清理再 upgrade。前 5 筆: {dups[:5]}"
        )

    # Task 22 加 unique constraint + ix_attendance_leave_record_id
    # Task 22 加 bad_leaves check
    # Task 23 加 _run_backfill


def downgrade():
    op.drop_constraint("fk_attendance_leave", "attendances", type_="foreignkey")
    op.drop_column("attendances", "partial_leave_hours")
    op.drop_column("attendances", "leave_record_id")
