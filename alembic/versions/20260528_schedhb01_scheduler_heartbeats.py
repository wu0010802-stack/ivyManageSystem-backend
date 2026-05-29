"""scheduler_heartbeats table + seed 14 rows

Revision ID: schedhb01
Revises: intghealth01
Create Date: 2026-05-28

Seeds 14 rows matching every scheduler_iteration() call site in services/:

| name                       | interval(s) | source                                                          |
|----------------------------|-------------|-----------------------------------------------------------------|
| activity_waitlist          | 300         | services/activity_waitlist_scheduler.py                         |
| medication_reminder        | 300         | services/medication_reminder_scheduler.py                       |
| auto_graduation            | 3600        | services/graduation_scheduler.py                                |
| salary_snapshot            | 86400       | services/salary_snapshot_scheduler.py                           |
| official_calendar          | 86400       | services/official_calendar_scheduler.py                         |
| finance_reconciliation     | 60          | services/finance_reconciliation_scheduler.py（tick CHECK_INTERVAL_SECONDS=60）|
| recruitment_term_advance   | 86400       | services/recruitment_term_advance_scheduler.py                  |
| pii_retention              | 86400       | services/pii_retention_scheduler.py                             |
| security_rate_limit_gc     | 300         | services/security_gc_scheduler.py                               |
| security_jwt_blocklist_gc  | 21600       | services/security_gc_scheduler.py（6 小時）                      |
| leave_quota_expiry         | 3600        | services/leave_quota_expiry_scheduler.py                        |
| line_token_health          | 86400       | services/line_token_health_scheduler.py                         |
| notification_retry         | 300         | services/notification/retry_scheduler.py                        |
| pending_uploads            | 300         | services/notification/pending_uploads_scheduler.py              |

interval 取自 config/scheduler.py 預設或 module-level 常數。執行 tick 但內部
業務未到觸發時刻（例如 medication_reminder 在非觸發時間 early return）仍算
成功 tick → heartbeat last_success_at 更新。lag 判斷以 2 × interval 為 threshold。

初始 last_success_at=NULL：第一次 tick 成功時 runtime UPDATE。/health/schedulers
針對 NULL 視為「啟動後尚未跑過」回 200 不告警。
"""

from alembic import op
import sqlalchemy as sa

revision = "schedhb01"
down_revision = "intghealth01"
branch_labels = None
depends_on = None


SCHEDULER_INTERVALS = {
    "activity_waitlist": 300,
    "medication_reminder": 300,
    "auto_graduation": 3600,
    "salary_snapshot": 86400,
    "official_calendar": 86400,
    "finance_reconciliation": 60,
    "recruitment_term_advance": 86400,
    "pii_retention": 86400,
    "security_rate_limit_gc": 300,
    "security_jwt_blocklist_gc": 21600,
    "leave_quota_expiry": 3600,
    "line_token_health": 86400,
    "notification_retry": 300,
    "pending_uploads": 300,
}


def upgrade() -> None:
    op.create_table(
        "scheduler_heartbeats",
        sa.Column("scheduler_name", sa.String(64), primary_key=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_error_message", sa.Text, nullable=True),
        sa.Column("expected_interval_seconds", sa.Integer, nullable=False),
        sa.Column(
            "last_rows_processed",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    for name, interval in SCHEDULER_INTERVALS.items():
        op.execute(
            sa.text(
                "INSERT INTO scheduler_heartbeats "
                "(scheduler_name, expected_interval_seconds, "
                "consecutive_failures, last_rows_processed, updated_at) "
                "VALUES (:n, :i, 0, 0, NOW())"
            ).bindparams(n=name, i=interval)
        )


def downgrade() -> None:
    op.drop_table("scheduler_heartbeats")
