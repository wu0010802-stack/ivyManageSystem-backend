"""create salary_calc_jobs table for DB-backed async job registry

原 in-process registry (services/salary_job_registry.py) 在多 worker 部署下：
- worker A 建立 job 後 worker B 查詢不到（404 機率 ~ 1 − 1/N）
- find_active() 無法跨 worker，同 year/month 可能被多 worker 同時觸發

此表提供 DB-backed 儲存，讓 registry 介面不變但狀態跨 worker 共享。

欄位設計對應 dataclass SalaryCalcJob；results / errors 以 JSON text 存放
（完成時一次寫入，進度更新期間不寫）。

Revision ID: l0g1h2i3j4k5
Revises: k9f0a1b2c3d4
Create Date: 2026-04-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "l0g1h2i3j4k5"
down_revision = "k9f0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "salary_calc_jobs" in inspect(bind).get_table_names():
        return

    op.create_table(
        "salary_calc_jobs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(32), nullable=False, unique=True),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("month", sa.Integer, nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("done", sa.Integer, nullable=False, server_default="0"),
        sa.Column("current_employee", sa.String(100), server_default=""),
        sa.Column("results_json", sa.Text(), nullable=True),
        sa.Column("errors_json", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("finished_at", sa.DateTime, nullable=True),
    )
    op.create_index(
        "ix_salary_calc_jobs_ym_status",
        "salary_calc_jobs",
        ["year", "month", "status"],
    )
    op.create_index(
        "ix_salary_calc_jobs_job_id",
        "salary_calc_jobs",
        ["job_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "salary_calc_jobs" not in inspect(bind).get_table_names():
        return
    op.drop_index("ix_salary_calc_jobs_job_id", table_name="salary_calc_jobs")
    op.drop_index("ix_salary_calc_jobs_ym_status", table_name="salary_calc_jobs")
    op.drop_table("salary_calc_jobs")
