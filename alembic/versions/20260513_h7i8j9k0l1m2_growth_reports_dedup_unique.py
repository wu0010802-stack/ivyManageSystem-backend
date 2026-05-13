"""growth_reports partial unique index for dedup (F-V6-02)

封死 create_growth_report 並發雙擊建出兩筆同 period 報告的 race。
admin 連點 POST → 兩個 BG task 跑 → 兩份 PDF → 兩個 send-line 端點分別走，
繞過 F-V6-01 的 LINE 5 分鐘冪等鎖（兩個 row 各自獨立）。

Partial unique index：同 (student_id, period_label, period_start, period_end) 在
status != 'failed' 範圍內最多一筆。'failed' 可重建，因為使用者本就需要 retry。

Revision ID: h7i8j9k0l1m2
Revises: g6h7i8j9k0l1
Create Date: 2026-05-13
"""

from alembic import op
import sqlalchemy as sa

revision = "h7i8j9k0l1m2"
down_revision = "g6h7i8j9k0l1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_growth_reports_period_active",
        "student_growth_reports",
        ["student_id", "period_label", "period_start", "period_end"],
        unique=True,
        postgresql_where=sa.text("status != 'failed'"),
        sqlite_where=sa.text("status != 'failed'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_growth_reports_period_active", table_name="student_growth_reports"
    )
