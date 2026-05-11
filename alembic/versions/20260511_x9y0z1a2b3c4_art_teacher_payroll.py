"""新增才藝老師薪資明細表 art_teacher_payroll_entries

對齊《義華薪資》才藝老師 sheet，每月每老師可有多筆給付：
- Vadim 外師 25h × 620 = 16120 + 課後美語(二) 4h × 620 = 2480
- 黃毓慧 美語 36.5h × 530 = 19345 + 加給活動 530 = 19875
- 鍾馨瑶 舞蹈 4h × 1000 = 4000 + 超額 200 = 4200

員工必須先在 employees 表，employee_type='hourly'。
salary engine 在 _build_breakdown_for_month 偵測該月 entries 存在時，
會以 sum(entries.total_amount) 覆寫 salary_record.hourly_total。

Revision ID: x9y0z1a2b3c4
Revises: w8x9y0z1a2b3
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "x9y0z1a2b3c4"
down_revision = "w8x9y0z1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "art_teacher_payroll_entries" in inspector.get_table_names():
        return

    op.create_table(
        "art_teacher_payroll_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("salary_year", sa.Integer(), nullable=False),
        sa.Column("salary_month", sa.Integer(), nullable=False),
        sa.Column(
            "subject",
            sa.String(50),
            nullable=False,
            comment="科目（美語/體能/舞蹈/管家/外師/感統 等）",
        ),
        sa.Column(
            "classroom_label",
            sa.String(50),
            nullable=True,
            comment="班級/星期備註（如「向.滿」「(二)」「(三.五)」）",
        ),
        sa.Column(
            "hours",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
            comment="時數（足球科目時可代表學生人數，業主自定）",
        ),
        sa.Column(
            "hourly_rate",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
            comment="鐘點費（每小時或每位幼生單價）",
        ),
        sa.Column(
            "base_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
            comment="小計 = hours × hourly_rate",
        ),
        sa.Column(
            "excess_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
            comment="超額（人數超出基準的加給，業主手動填）",
        ),
        sa.Column(
            "activity_bonus",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
            comment="加給活動（自由加給：黃毓慧 530、李麗珍 6000）",
        ),
        sa.Column(
            "total_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
            comment="總計 = base_amount + excess_amount + activity_bonus",
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.String(50), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_by", sa.String(50), nullable=True),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_art_teacher_payroll_emp_month",
        "art_teacher_payroll_entries",
        ["employee_id", "salary_year", "salary_month"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "art_teacher_payroll_entries" not in inspector.get_table_names():
        return
    op.drop_index(
        "ix_art_teacher_payroll_emp_month",
        table_name="art_teacher_payroll_entries",
    )
    op.drop_table("art_teacher_payroll_entries")
