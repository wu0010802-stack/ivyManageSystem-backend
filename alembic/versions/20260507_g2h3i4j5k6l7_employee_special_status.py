"""employees 加特殊投保/獎金狀態欄位（階段 2-C）

加 5 個欄位讓系統能表達常見會計實務狀況：

1. `no_employment_insurance` (Bool) — 退休再僱用，免就保。勞保只算 11.5% 不含 1% 就保。
2. `health_exempt` (Bool) — 健保由其他管道（公保/老人健保等）；公司不扣健保。
3. `skip_payroll_bonuses` (Bool) — 業主指示「不發紅利/節慶/超額獎金」（如總園長指示
   不薪轉、不作帳的特殊個案）。基本薪 + 勞健保仍正常計算。
4. `extra_dependents_quarterly` (Int) — 第 2+ 名眷屬季扣模式。dependents 仍是月扣眷屬數
   （業主實務通常 1）；本欄位為「額外季扣」眷屬人數，會在 1/4/7/10 月份額外加扣
   `health_employee × extra_dependents_quarterly × 3`。
5. `insurance_salary_override_reason` (String) — 純文字記錄「為何投保金額 ≠ 底薪」，
   合規證明用。不影響計算。

Why: 義華 115.04 對齊任務（2026-05-06）發現的 schema 缺口。原本只能靠人工 Excel
標註，無法自動算對林姿妙、吳逸喬、蔡佩汶等個案。

Revision ID: g2h3i4j5k6l7
Revises: f1g2h3i4j5k6
Create Date: 2026-05-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "g2h3i4j5k6l7"
down_revision = "f1g2h3i4j5k6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "employees" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("employees")}

    if "no_employment_insurance" not in cols:
        op.add_column(
            "employees",
            sa.Column(
                "no_employment_insurance",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
                comment="免就保（退休再聘等）；勞保扣款改用 11.5% 不含就保 1%",
            ),
        )
    if "health_exempt" not in cols:
        op.add_column(
            "employees",
            sa.Column(
                "health_exempt",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
                comment="健保豁免（公保/老人健保等）；公司不扣本人+眷屬健保",
            ),
        )
    if "skip_payroll_bonuses" not in cols:
        op.add_column(
            "employees",
            sa.Column(
                "skip_payroll_bonuses",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
                comment="業主指示不發紅利/節慶/超額獎金（基本薪+保險仍正常計算）",
            ),
        )
    if "extra_dependents_quarterly" not in cols:
        op.add_column(
            "employees",
            sa.Column(
                "extra_dependents_quarterly",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
                comment="季扣眷屬人數；1/4/7/10 月份額外扣 health_employee × N × 3",
            ),
        )
    if "insurance_salary_override_reason" not in cols:
        op.add_column(
            "employees",
            sa.Column(
                "insurance_salary_override_reason",
                sa.String(200),
                nullable=True,
                comment="投保金額 ≠ 底薪 的合規記錄；純文字，不影響計算",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "employees" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("employees")}
    for col in (
        "no_employment_insurance",
        "health_exempt",
        "skip_payroll_bonuses",
        "extra_dependents_quarterly",
        "insurance_salary_override_reason",
    ):
        if col in cols:
            op.drop_column("employees", col)
