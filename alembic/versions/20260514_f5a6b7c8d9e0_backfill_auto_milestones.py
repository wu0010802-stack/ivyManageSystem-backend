"""backfill auto milestones for all active students

對所有 lifecycle_status in ('active', 'graduated') 的學生套自動偵測規則：
- first_day: 從 enrollment_date 建立
- birthday: 從 birthday 建立過去所有滿歲生日
- graduation: 從 graduation_date（僅 graduated 狀態）
- perfect_attendance_month: 掃 attendance records

注意：本 migration 大量寫入，依 student count 可能需要數分鐘。
INSERT ... ON CONFLICT 因為 partial unique index 限制只在 PG 生效（含 SQLite test）。

Revision ID: f5a6b7c8d9e0
Revises: d3e4f5a6b7c8
Create Date: 2026-05-14
"""

import logging
import os
import sys
from datetime import date

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "f5a6b7c8d9e0"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    """M1 改造：原 N+1 SELECT attendance（每位學生一次）改為單發批撈 + group by。
    Production 1000+ 學生 × 數年資料原本可能跑數十分鐘並鎖 student_attendances。
    本版預期 < 1 分鐘完成，且每 100 學生 commit 一次，支援中斷後重跑（ON CONFLICT
    DO NOTHING 保證冪等）。
    """
    # 確保可以 import services.milestone_detector
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    from services.milestone_detector import (
        detect_birthdays,
        detect_first_day,
        detect_graduation,
        detect_perfect_attendance_months,
    )

    today = date.today()
    conn = op.get_bind()
    student_rows = conn.execute(text("""
            SELECT id, birthday, enrollment_date, graduation_date, lifecycle_status
            FROM students
            WHERE lifecycle_status IN ('active', 'graduated')
            ORDER BY id
        """)).fetchall()

    class _S:
        def __init__(self, row):
            self.id = row[0]
            self.birthday = row[1]
            self.enrollment_date = row[2]
            self.graduation_date = row[3]
            self.lifecycle_status = row[4]

    # M1 改造：一次撈完所有 lifecycle in ('active', 'graduated') 學生的考勤紀錄，
    # group by student_id 取代原本每位學生獨立 SELECT 的 N+1。
    att_by_student: dict[int, list[dict]] = {}
    if student_rows:
        all_att = conn.execute(text("""
                SELECT sa.student_id, sa.date, sa.status
                FROM student_attendances sa
                JOIN students s ON s.id = sa.student_id
                WHERE s.lifecycle_status IN ('active', 'graduated')
                ORDER BY sa.student_id, sa.date
            """)).fetchall()
        for sid, d, status in all_att:
            att_by_student.setdefault(sid, []).append({"date": d, "status": status})

    BATCH_COMMIT_SIZE = 100
    total_attempted = 0
    total_inserted = 0
    for i, row in enumerate(student_rows, 1):
        s = _S(row)
        payloads = []
        payloads.extend(detect_first_day(s))
        payloads.extend(detect_birthdays(s, today))
        payloads.extend(detect_graduation(s))

        att_records = att_by_student.get(s.id, [])
        payloads.extend(detect_perfect_attendance_months(s.id, att_records, today))

        for p in payloads:
            inserted = _try_insert(conn, p)
            total_attempted += 1
            if inserted:
                total_inserted += 1

        # 每 BATCH_COMMIT_SIZE 學生 commit 一次：分散長交易，中斷後可重跑
        # （INSERT ... ON CONFLICT DO NOTHING 保證冪等）。
        if i % BATCH_COMMIT_SIZE == 0:
            conn.commit()
            logger.info(
                "backfill auto milestones: 已處理 %d 學生（小計 inserted=%d）",
                i,
                total_inserted,
            )

    conn.commit()
    logger.info(
        "backfill auto milestones done: 共嘗試 %d 筆，實際插入 %d 筆",
        total_attempted,
        total_inserted,
    )


def _try_insert(conn, p) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING。回傳 True 表示實際插入了一筆。"""
    result = conn.execute(
        text("""
            INSERT INTO student_milestones (
                student_id, milestone_type, achieved_on, title, description,
                icon, source_type, source_ref_type, source_ref_id,
                created_at, updated_at
            ) VALUES (
                :student_id, :milestone_type, :achieved_on, :title, :description,
                :icon, :source_type, :source_ref_type, :source_ref_id,
                NOW(), NOW()
            )
            ON CONFLICT DO NOTHING
        """),
        {
            "student_id": p["student_id"],
            "milestone_type": p["milestone_type"],
            "achieved_on": p["achieved_on"],
            "title": p["title"],
            "description": p.get("description"),
            "icon": p.get("icon"),
            "source_type": p["source_type"],
            "source_ref_type": p.get("source_ref_type"),
            "source_ref_id": p.get("source_ref_id"),
        },
    )
    return result.rowcount > 0


def downgrade() -> None:
    """刪除所有 auto_* source_type 的 milestones."""
    op.execute(text("""
            DELETE FROM student_milestones
            WHERE source_type IN (
                'auto_enrollment', 'auto_attendance',
                'auto_observation', 'auto_assessment'
            )
        """))
