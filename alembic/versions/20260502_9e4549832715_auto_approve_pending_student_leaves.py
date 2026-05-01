"""auto approve pending student leaves and backfill attendance

Revision ID: 9e4549832715
Revises: o0l1m2n3o4p5
Create Date: 2026-05-02

家長端學生請假改為提交即自動核准。為相容舊資料，把所有 status='pending'
的 StudentLeaveRequest 一次性轉為 'approved' 並補寫 StudentAttendance。

attendance 寫入規則：
- 應到日 = 排除 weekend / holiday，但保留 makeup workday
- 衝突時保留原 recorded_by，覆蓋 status / remark
- recorded_by 寫 NULL（系統自動寫入）

Downgrade no-op（無法還原 pending 狀態與 attendance 衝突前的 status）。
"""

from datetime import date, datetime, timedelta
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

# 保留 alembic 自動產生的 revision / down_revision
revision: str = "9e4549832715"
down_revision: Union[str, Sequence[str], None] = "o0l1m2n3o4p5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _classify_workday(target: date, holiday_set: set, makeup_set: set) -> bool:
    """回傳該日是否為「應到日」。"""
    if target in makeup_set:
        return True
    if target in holiday_set:
        return False
    return target.weekday() < 5


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "student_leave_requests" not in inspector.get_table_names():
        return

    pending_rows = bind.execute(
        sa.text(
            "SELECT id, student_id, leave_type, start_date, end_date "
            "FROM student_leave_requests WHERE status = 'pending'"
        )
    ).fetchall()
    if not pending_rows:
        return

    now = datetime.now()
    for row in pending_rows:
        leave_id = row[0]
        student_id = row[1]
        leave_type = row[2]
        start_d = (
            row[3] if isinstance(row[3], date) else date.fromisoformat(str(row[3]))
        )
        end_d = row[4] if isinstance(row[4], date) else date.fromisoformat(str(row[4]))

        # 載入區間內 holiday / makeup
        holiday_dates = bind.execute(
            sa.text(
                "SELECT date FROM holidays WHERE date >= :s AND date <= :e AND is_active = TRUE"
            ),
            {"s": start_d, "e": end_d},
        ).fetchall()
        makeup_dates = bind.execute(
            sa.text(
                "SELECT date FROM workday_overrides WHERE date >= :s AND date <= :e AND is_active = TRUE"
            ),
            {"s": start_d, "e": end_d},
        ).fetchall()
        holiday_set = {
            (r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0])))
            for r in holiday_dates
        }
        makeup_set = {
            (r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0])))
            for r in makeup_dates
        }

        cur = start_d
        while cur <= end_d:
            if _classify_workday(cur, holiday_set, makeup_set):
                existing = bind.execute(
                    sa.text(
                        "SELECT id FROM student_attendances "
                        "WHERE student_id = :sid AND date = :d"
                    ),
                    {"sid": student_id, "d": cur},
                ).fetchone()
                remark = f"家長申請#{leave_id}"
                if existing is None:
                    bind.execute(
                        sa.text(
                            "INSERT INTO student_attendances "
                            "(student_id, date, status, remark, recorded_by, created_at, updated_at) "
                            "VALUES (:sid, :d, :st, :rm, NULL, :now, :now)"
                        ),
                        {
                            "sid": student_id,
                            "d": cur,
                            "st": leave_type,
                            "rm": remark,
                            "now": now,
                        },
                    )
                else:
                    bind.execute(
                        sa.text(
                            "UPDATE student_attendances SET status = :st, remark = :rm, updated_at = :now "
                            "WHERE id = :aid"
                        ),
                        {
                            "st": leave_type,
                            "rm": remark,
                            "now": now,
                            "aid": existing[0],
                        },
                    )
            cur += timedelta(days=1)

        bind.execute(
            sa.text(
                "UPDATE student_leave_requests SET status = 'approved', "
                "reviewed_at = :now, reviewed_by = NULL, updated_at = :now "
                "WHERE id = :lid"
            ),
            {"now": now, "lid": leave_id},
        )


def downgrade() -> None:
    # no-op：無法還原原本的 pending 狀態與 attendance 衝突前的內容
    pass
