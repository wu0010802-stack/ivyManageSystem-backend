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

    total_attempted = 0
    total_inserted = 0
    for i, row in enumerate(student_rows, 1):
        s = _S(row)
        payloads = []
        payloads.extend(detect_first_day(s))
        payloads.extend(detect_birthdays(s, today))
        payloads.extend(detect_graduation(s))

        att_rows = conn.execute(
            text(
                "SELECT date, status FROM student_attendances "
                "WHERE student_id = :sid"
            ),
            {"sid": s.id},
        ).fetchall()
        att_records = [{"date": a[0], "status": a[1]} for a in att_rows]
        payloads.extend(detect_perfect_attendance_months(s.id, att_records, today))

        for p in payloads:
            inserted = _try_insert(conn, p)
            total_attempted += 1
            if inserted:
                total_inserted += 1

        if i % 100 == 0:
            logger.info("backfill auto milestones: 已處理 %d 學生", i)

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
