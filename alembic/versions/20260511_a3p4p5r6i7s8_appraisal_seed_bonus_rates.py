"""appraisal: seed 6 筆考核獎金率（115.01.01 effective）

來源：第六篇 考核辦法第四條 + 附表八。

3 個 role_group × 2 個等級（優 OUTSTANDING / 甲 GOOD）= 6 筆。
乙以下 PASS/WARN/FAIL 不發獎金，故無 seed。

實際金額（per spec 4.5）：
- 主管 SUPERVISOR：優 10000 / 甲 5000
- 班導/會計 HEAD_TEACHER：優 8000 / 甲 4000
- 副班導/廚/司機/儲備 ASSISTANT：優 6000 / 甲 3500

獎金計算：bonus_amount = base_amount × (total_score / 100)

Revision ID: a3p4p5r6i7s8
Revises: a7p8p9r0i1s2
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op

revision = "a3p4p5r6i7s8"
down_revision = "a7p8p9r0i1s2"
branch_labels = None
depends_on = None

EFFECTIVE = "2026-08-01"  # 115 學年度第一學期起算

RATES = [
    # (role_group, grade, base_amount)
    ("SUPERVISOR", "OUTSTANDING", 10000),
    ("SUPERVISOR", "GOOD", 5000),
    ("HEAD_TEACHER", "OUTSTANDING", 8000),
    ("HEAD_TEACHER", "GOOD", 4000),
    ("ASSISTANT", "OUTSTANDING", 6000),
    ("ASSISTANT", "GOOD", 3500),
]


def upgrade() -> None:
    bind = op.get_bind()
    for role_group, grade, amount in RATES:
        bind.execute(
            sa.text(
                "INSERT INTO appraisal_bonus_rates "
                "(effective_from, role_group, grade, base_amount) "
                "VALUES (:eff, :rg, :gr, :amt) "
                "ON CONFLICT (effective_from, role_group, grade) DO NOTHING"
            ),
            {"eff": EFFECTIVE, "rg": role_group, "gr": grade, "amt": amount},
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM appraisal_bonus_rates WHERE effective_from = :eff"),
        {"eff": EFFECTIVE},
    )
