"""employees 加勞保 / 健保 / 勞退 分項投保金額（議題 B）

讓單一員工的三制度可投保不同金額，解開以下實務 case：
- 王品嬑：勞保 29500（級距上限/業主協議）/ 健保 30300（合約底薪級距）
- 林姿妙：勞退提繳工資 45800（與勞保上限對齊）/ 但 base 46499 落 48200 級距

Schema 設計：三個欄位皆 nullable；NULL=fallback 沿用 `insurance_salary_level`，
維持向後相容。InsuranceService.calculate 接受 kwargs `labor_insured` /
`health_insured` / `pension_insured`，None 自動套 salary。

Why: 議題 A 解了「個人 vs 職位標準底薪」分歧；議題 B 解「同一人三制度不同投保」。
兩者正交但同樣高頻，業主常見會「健保有 / 勞保無」（外籍補助）或「勞退降至上限對齊勞保」。

Revision ID: j5k6l7m8n9o0
Revises: i4j5k6l7m8n9
Create Date: 2026-05-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "j5k6l7m8n9o0"
down_revision = "i4j5k6l7m8n9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "employees" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("employees")}
    for col in (
        "labor_insured_salary",
        "health_insured_salary",
        "pension_insured_salary",
    ):
        if col in cols:
            continue
        op.add_column(
            "employees",
            sa.Column(
                col,
                sa.Numeric(12, 2),
                nullable=True,
                comment=(
                    f"{col.split('_')[0]} 制度獨立投保金額；NULL=沿用 insurance_salary_level"
                ),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "employees" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("employees")}
    for col in (
        "labor_insured_salary",
        "health_insured_salary",
        "pension_insured_salary",
    ):
        if col in cols:
            op.drop_column("employees", col)
