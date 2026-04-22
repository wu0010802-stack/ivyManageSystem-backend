"""create salary_snapshots table — 月底薪資快照機制

SalaryRecord 為可變工作副本（每次重算覆蓋），
SalarySnapshot 為不可變歷史快照：
- month_end（月底自動）
- finalize（封存時同步寫入）
- manual（管理員手動補拍）

欄位結構與 SalaryRecord 金額/計數/布林/remark 一致，
外加 snapshot_type / captured_at / captured_by / source_version / snapshot_remark。

Revision ID: u9p0q1r2s3t4
Revises: t8o9p0q1r2s3
Create Date: 2026-04-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

from models.types import Money

revision = "u9p0q1r2s3t4"
down_revision = "t8o9p0q1r2s3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "salary_snapshots" in inspect(bind).get_table_names():
        return

    op.create_table(
        "salary_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "salary_record_id",
            sa.Integer,
            sa.ForeignKey("salary_records.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("employee_id", sa.Integer, nullable=False),
        sa.Column("salary_year", sa.Integer, nullable=False),
        sa.Column("salary_month", sa.Integer, nullable=False),
        # 金額/計數/布林/備註欄位（與 SalaryRecord 對齊）
        sa.Column("base_salary", Money, server_default="0"),
        sa.Column("festival_bonus", Money, server_default="0"),
        sa.Column("overtime_bonus", Money, server_default="0"),
        sa.Column("performance_bonus", Money, server_default="0"),
        sa.Column("special_bonus", Money, server_default="0"),
        sa.Column("overtime_pay", Money, server_default="0"),
        sa.Column("meeting_overtime_pay", Money, server_default="0"),
        sa.Column("meeting_absence_deduction", Money, server_default="0"),
        sa.Column("birthday_bonus", Money, server_default="0"),
        sa.Column("work_hours", sa.Float, server_default="0"),
        sa.Column("hourly_rate", Money, server_default="0"),
        sa.Column("hourly_total", Money, server_default="0"),
        sa.Column("labor_insurance_employee", Money, server_default="0"),
        sa.Column("labor_insurance_employer", Money, server_default="0"),
        sa.Column("health_insurance_employee", Money, server_default="0"),
        sa.Column("health_insurance_employer", Money, server_default="0"),
        sa.Column("pension_employee", Money, server_default="0"),
        sa.Column("pension_employer", Money, server_default="0"),
        sa.Column("late_deduction", Money, server_default="0"),
        sa.Column("early_leave_deduction", Money, server_default="0"),
        sa.Column("missing_punch_deduction", Money, server_default="0"),
        sa.Column("leave_deduction", Money, server_default="0"),
        sa.Column("absence_deduction", Money, server_default="0"),
        sa.Column("other_deduction", Money, server_default="0"),
        sa.Column("late_count", sa.Integer, server_default="0"),
        sa.Column("early_leave_count", sa.Integer, server_default="0"),
        sa.Column("missing_punch_count", sa.Integer, server_default="0"),
        sa.Column("absent_count", sa.Integer, server_default="0"),
        sa.Column("gross_salary", Money, server_default="0"),
        sa.Column("total_deduction", Money, server_default="0"),
        sa.Column("net_salary", Money, server_default="0"),
        sa.Column(
            "bonus_separate",
            sa.Boolean,
            server_default=sa.text("false"),
        ),
        sa.Column("bonus_amount", Money, server_default="0"),
        sa.Column("supervisor_dividend", Money, server_default="0"),
        sa.Column("remark", sa.Text, nullable=True),
        # 快照 metadata
        sa.Column("snapshot_type", sa.String(20), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("captured_by", sa.String(50), nullable=True),
        sa.Column("source_version", sa.Integer, nullable=True),
        sa.Column("snapshot_remark", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_salary_snapshot_ym",
        "salary_snapshots",
        ["salary_year", "salary_month"],
    )
    op.create_index(
        "ix_salary_snapshot_emp_ym",
        "salary_snapshots",
        ["employee_id", "salary_year", "salary_month"],
    )
    op.create_index(
        "ix_salary_snapshot_ym_type",
        "salary_snapshots",
        ["salary_year", "salary_month", "snapshot_type"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "salary_snapshots" not in inspect(bind).get_table_names():
        return
    op.drop_index("ix_salary_snapshot_ym_type", table_name="salary_snapshots")
    op.drop_index("ix_salary_snapshot_emp_ym", table_name="salary_snapshots")
    op.drop_index("ix_salary_snapshot_ym", table_name="salary_snapshots")
    op.drop_table("salary_snapshots")
