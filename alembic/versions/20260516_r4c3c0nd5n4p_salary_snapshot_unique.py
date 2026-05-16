"""race condition guards (snapshot / calc job / non-monthly fee partial unique)

Revision ID: r4c3c0nd5n4p
Revises: adj1stmnt001
Create Date: 2026-05-16

三條跨 worker race condition 防護：

1. salary_snapshots：去重 (emp, ym, snapshot_type) 重複的 month_end/finalize 後，
   建立 partial unique index。阻擋 lazy trigger + scheduler 撞同秒雙 INSERT。
   manual 類型允許重複（管理員可手動補拍）。

2. salary_calc_jobs：建立 partial unique index 限制同 (year, month) 同時只能
   有一筆 status IN (pending, running) 的 job。阻擋 find→create 的 TOCTOU；
   registry create() 用 IntegrityError 接住回 409。

3. student_fee_records 非月費類型：建立 partial unique index (student_id,
   source_template_id, period) WHERE target_month IS NULL，阻擋並發 generate
   重複建立註冊費 / 制服費 / 學費等非月費記錄。月費另由
   ix_fee_records_monthly_unique 守護不重複。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r4c3c0nd5n4p"
down_revision: Union[str, Sequence[str], None] = "adj1stmnt001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 去重：對於同 (emp, year, month, snapshot_type) 且 type in (month_end, finalize)
    #    的重複 row，僅保留最小 id；其餘刪除。
    op.execute("""
        DELETE FROM salary_snapshots
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY employee_id, salary_year, salary_month, snapshot_type
                           ORDER BY id ASC
                       ) AS rn
                FROM salary_snapshots
                WHERE snapshot_type IN ('month_end', 'finalize')
            ) t
            WHERE rn > 1
        )
        """)

    # 2) 建 salary_snapshots partial unique index
    op.create_index(
        "uq_salary_snapshot_emp_ym_immutable",
        "salary_snapshots",
        ["employee_id", "salary_year", "salary_month", "snapshot_type"],
        unique=True,
        postgresql_where=sa.text("snapshot_type IN ('month_end', 'finalize')"),
        sqlite_where=sa.text("snapshot_type IN ('month_end', 'finalize')"),
    )

    # 3) salary_calc_jobs：先去重（理論上 in-process lock 已防住，但保險起見），
    #    保留最新 created_at；其他 active 設為 failed 避免直接 DELETE 影響稽核。
    op.execute("""
        UPDATE salary_calc_jobs
        SET status = 'failed',
            error_message = COALESCE(error_message, '') ||
                ' [migration r4c3c0nd5n4p: 與其他 active job 衝突，於建立 unique index 前自動標記失敗]',
            finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY year, month
                           ORDER BY created_at DESC, id DESC
                       ) AS rn
                FROM salary_calc_jobs
                WHERE status IN ('pending', 'running')
            ) t
            WHERE rn > 1
        )
        """)

    # 4) 建 salary_calc_jobs partial unique index
    op.create_index(
        "uq_salary_calc_jobs_active_ym",
        "salary_calc_jobs",
        ["year", "month"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
        sqlite_where=sa.text("status IN ('pending', 'running')"),
    )

    # 5) student_fee_records 非月費去重：保留最早 id；其餘 DELETE。
    op.execute("""
        DELETE FROM student_fee_records
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY student_id, source_template_id, period
                           ORDER BY id ASC
                       ) AS rn
                FROM student_fee_records
                WHERE source_template_id IS NOT NULL AND target_month IS NULL
            ) t
            WHERE rn > 1
        )
        """)

    # 6) 建 student_fee_records 非月費 partial unique index
    op.create_index(
        "uq_fee_records_non_monthly_unique",
        "student_fee_records",
        ["student_id", "source_template_id", "period"],
        unique=True,
        postgresql_where=sa.text(
            "source_template_id IS NOT NULL AND target_month IS NULL"
        ),
        sqlite_where=sa.text("source_template_id IS NOT NULL AND target_month IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_fee_records_non_monthly_unique", table_name="student_fee_records")
    op.drop_index("uq_salary_calc_jobs_active_ym", table_name="salary_calc_jobs")
    op.drop_index("uq_salary_snapshot_emp_ym_immutable", table_name="salary_snapshots")
