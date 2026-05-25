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

    # 4. Online 加 unique constraint（CREATE UNIQUE INDEX CONCURRENTLY → ADD CONSTRAINT USING INDEX）
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_attendance_employee_date
            ON attendances (employee_id, attendance_date)
        """)
    op.execute("""
        ALTER TABLE attendances
        ADD CONSTRAINT uq_attendance_employee_date
        UNIQUE USING INDEX uq_attendance_employee_date
    """)
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_attendance_leave_record_id
            ON attendances (leave_record_id)
        """)

    # 5. Pre-flight validator：阻擋既有部分請假缺 start_time/end_time
    bad_leaves = conn.execute(text("""
        SELECT id, employee_id, start_date, leave_hours
        FROM leave_records
        WHERE is_approved = true
          AND end_date >= CURRENT_DATE - INTERVAL '12 months'
          AND (start_time IS NULL OR end_time IS NULL)
          AND (leave_hours IS NOT NULL AND leave_hours < 8)
    """)).fetchall()
    if bad_leaves:
        raise RuntimeError(
            f"偵測到 {len(bad_leaves)} 筆已核可的部分請假缺 start_time/end_time，"
            f"請先跑 scripts/fix_partial_leave_times.py 補時段或回到 pending 重審。"
            f"前 5 筆: {bad_leaves[:5]}"
        )

    # 6. Backfill(env IVY_SKIP_BACKFILL=1 可跳)
    import os

    if not os.getenv("IVY_SKIP_BACKFILL"):
        _run_backfill(conn)


def _run_backfill(conn) -> None:
    """把近 12 個月所有 is_approved=True 的請假記錄同步至 attendances。

    SAVEPOINT 單筆隔離：
    - LeaveAttendanceConflict → 放行 + 寫 audit_logs（不終止整批）
    - 其他例外 → fail-loud RuntimeError（終止 migration）
    進度每 100 筆 print 一次。
    """
    from services.employee_leave_attendance_sync import (
        apply,
        LeaveAttendanceConflict,
        LeaveNotApproved,
    )
    from sqlalchemy.orm import Session
    from models.audit import AuditLog

    result = conn.execute(text("""
            SELECT id
            FROM leave_records
            WHERE is_approved = true
              AND end_date >= CURRENT_DATE - INTERVAL '12 months'
            ORDER BY id
        """))
    leave_ids = [row[0] for row in result.fetchall()]

    total = len(leave_ids)
    print(f"[empleavesync backfill] 共 {total} 筆 approved leaves 待同步")

    session = Session(bind=conn)
    skipped_conflicts = 0
    applied_count = 0

    for i, lid in enumerate(leave_ids, start=1):
        sp = session.begin_nested()  # SAVEPOINT
        try:
            apply(session, lid)
            sp.commit()
            applied_count += 1
        except LeaveAttendanceConflict as exc:
            sp.rollback()
            skipped_conflicts += 1
            # 寫 audit_logs 記錄衝突
            audit = AuditLog(
                action="UPDATE",
                entity_type="leave_records",
                entity_id=str(lid),
                summary=f"[backfill] LeaveAttendanceConflict: {exc}",
            )
            session.add(audit)
            session.flush()
        except LeaveNotApproved:
            # 極罕見：查詢後被撤銷，忽略即可
            sp.rollback()
        except Exception as exc:
            sp.rollback()
            raise RuntimeError(
                f"[empleavesync backfill] leave_id={lid} 同步失敗，終止 backfill。原因: {exc}"
            ) from exc

        if i % 100 == 0 or i == total:
            print(
                f"[empleavesync backfill] 進度 {i}/{total}，"
                f"已套用 {applied_count}，衝突跳過 {skipped_conflicts}"
            )

    print(
        f"[empleavesync backfill] 完成。"
        f"套用 {applied_count} / 衝突跳過 {skipped_conflicts} / 共 {total}"
    )


def downgrade():
    op.drop_constraint("uq_attendance_employee_date", "attendances", type_="unique")
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_attendance_leave_record_id")
    op.drop_constraint("fk_attendance_leave", "attendances", type_="foreignkey")
    op.drop_column("attendances", "partial_leave_hours")
    op.drop_column("attendances", "leave_record_id")
