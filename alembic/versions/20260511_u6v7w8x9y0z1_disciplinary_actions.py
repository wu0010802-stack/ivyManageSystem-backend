"""新增懲處記錄表 + BonusConfig 懲處扣款預設欄位

實作會計慣例的「警告 -1000 / 小過 -3000 / 大過 -？」扣節慶/超額獎金機制。

- disciplinary_actions：每筆懲處（員工、日期、類型、金額、原因、抵扣狀態）
- bonus_configs 增 warning_deduction / minor_offense_deduction / major_offense_deduction
  作為 DisciplinaryAction.deduction_amount 為 0 時的 fallback 值。

Why: 義華薪資 115.04 對齊揭露的功能缺口（Excel 顯示呂宜凡警告-1000、林姿妙小過-3000、
郭碧婷大過記錄都直接出現在節慶獎金 sheet 上）。

Revision ID: u6v7w8x9y0z1
Revises: t5u6v7w8x9y0
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "u6v7w8x9y0z1"
down_revision = "t5u6v7w8x9y0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()

    # 1. disciplinary_actions 表
    if "disciplinary_actions" not in tables:
        op.create_table(
            "disciplinary_actions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("employee_id", sa.Integer(), nullable=False),
            sa.Column("action_date", sa.Date(), nullable=False, comment="懲處發生日"),
            sa.Column(
                "action_type",
                sa.String(20),
                nullable=False,
                comment="warning=警告 / minor=小過 / major=大過",
            ),
            sa.Column(
                "deduction_amount",
                sa.Numeric(12, 2),
                nullable=False,
                server_default=sa.text("0"),
                comment="扣款金額（0 表示用 BonusConfig 預設）",
            ),
            sa.Column("reason", sa.Text(), nullable=True, comment="懲處原因"),
            sa.Column(
                "applied_to_salary_id",
                sa.Integer(),
                nullable=True,
                comment="已抵扣的 salary_record id（NULL=尚未抵扣）",
            ),
            sa.Column(
                "applied_at",
                sa.DateTime(),
                nullable=True,
                comment="實際抵扣時間",
            ),
            sa.Column(
                "applied_amount",
                sa.Numeric(12, 2),
                nullable=True,
                comment="實際抵扣金額（可能因獎金不足而少於 deduction_amount）",
            ),
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
            sa.ForeignKeyConstraint(
                ["employee_id"], ["employees.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["applied_to_salary_id"],
                ["salary_records.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_disciplinary_actions_employee_date",
            "disciplinary_actions",
            ["employee_id", "action_date"],
        )
        op.create_index(
            "ix_disciplinary_actions_pending",
            "disciplinary_actions",
            ["employee_id", "applied_to_salary_id"],
        )

    # 2. bonus_configs 加 3 個懲處扣款預設欄位
    if "bonus_configs" in tables:
        cols = {c["name"] for c in inspector.get_columns("bonus_configs")}
        if "warning_deduction" not in cols:
            op.add_column(
                "bonus_configs",
                sa.Column(
                    "warning_deduction",
                    sa.Float(),
                    nullable=False,
                    server_default=sa.text("1000"),
                    comment="警告一支預設扣款（從節慶/超額獎金扣）",
                ),
            )
        if "minor_offense_deduction" not in cols:
            op.add_column(
                "bonus_configs",
                sa.Column(
                    "minor_offense_deduction",
                    sa.Float(),
                    nullable=False,
                    server_default=sa.text("3000"),
                    comment="小過一支預設扣款",
                ),
            )
        if "major_offense_deduction" not in cols:
            op.add_column(
                "bonus_configs",
                sa.Column(
                    "major_offense_deduction",
                    sa.Float(),
                    nullable=False,
                    server_default=sa.text("0"),
                    comment="大過一支預設扣款（業主未定，預設 0 由個別案件指定）",
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()

    if "bonus_configs" in tables:
        cols = {c["name"] for c in inspector.get_columns("bonus_configs")}
        for col_name in (
            "warning_deduction",
            "minor_offense_deduction",
            "major_offense_deduction",
        ):
            if col_name in cols:
                op.drop_column("bonus_configs", col_name)

    if "disciplinary_actions" in tables:
        op.drop_index(
            "ix_disciplinary_actions_pending",
            table_name="disciplinary_actions",
        )
        op.drop_index(
            "ix_disciplinary_actions_employee_date",
            table_name="disciplinary_actions",
        )
        op.drop_table("disciplinary_actions")
