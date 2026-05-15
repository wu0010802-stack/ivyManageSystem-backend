"""appraisal: seed 10 筆考核獎金率（M1 重構：擴充至 5 個 role_group × 2 等級）

對應 Excel「114(上)年度考核統計表」備註：
- 優等 90+：園長/主任 8000、教師/行政會計、副班導等依群分；獎金 = base × 分數%
- 甲等 80-89：園長/主任 5000；其他群依比例

Excel 中註解被截斷未明示 STAFF/COOK 金額；本 seed 採合理預設，可由
appraisal_bonus_rates 的 effective_from 機制新增覆蓋。

5 role_group × 2 grade (OUTSTANDING/GOOD) = 10 筆；
PASS/WARN/FAIL 不發獎金故無 seed。

Revision ID: a3p4p5r6i7s8
Revises: a7p8p9r0i1s2
Create Date: 2026-05-11 (rewritten 2026-05-15 for M1)
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
    ("SUPERVISOR", "OUTSTANDING", 8000),
    ("SUPERVISOR", "GOOD", 5000),
    ("HEAD_TEACHER", "OUTSTANDING", 6000),
    ("HEAD_TEACHER", "GOOD", 4000),
    ("ASSISTANT", "OUTSTANDING", 4500),
    ("ASSISTANT", "GOOD", 3000),
    ("STAFF", "OUTSTANDING", 5000),
    ("STAFF", "GOOD", 3500),
    ("COOK", "OUTSTANDING", 3500),
    ("COOK", "GOOD", 2500),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "appraisal_bonus_rates" not in inspector.get_table_names():
        return

    bonus_table = sa.table(
        "appraisal_bonus_rates",
        sa.column("effective_from", sa.Date),
        sa.column("role_group", sa.String),
        sa.column("grade", sa.String),
        sa.column("base_amount", sa.Numeric),
    )

    # 冪等：先查已存在的 (effective_from, role_group, grade) 三元組
    existing = {
        (row[0].isoformat(), row[1], row[2])
        for row in bind.execute(
            sa.text(
                "SELECT effective_from, role_group, grade "
                "FROM appraisal_bonus_rates "
                "WHERE effective_from = :ef"
            ),
            {"ef": EFFECTIVE},
        ).fetchall()
    }

    rows_to_insert = []
    for role_group, grade, base_amount in RATES:
        if (EFFECTIVE, role_group, grade) in existing:
            continue
        rows_to_insert.append(
            {
                "effective_from": EFFECTIVE,
                "role_group": role_group,
                "grade": grade,
                "base_amount": base_amount,
            }
        )
    if rows_to_insert:
        op.bulk_insert(bonus_table, rows_to_insert)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "appraisal_bonus_rates" not in inspector.get_table_names():
        return
    bind.execute(
        sa.text(
            "DELETE FROM appraisal_bonus_rates WHERE effective_from = :ef"
        ),
        {"ef": EFFECTIVE},
    )
