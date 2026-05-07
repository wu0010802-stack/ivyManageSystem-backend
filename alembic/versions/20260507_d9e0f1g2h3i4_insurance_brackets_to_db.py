"""insurance brackets → DB（級距表落地）

把原本 hardcode 在 services/insurance_service.py 的 INSURANCE_TABLE_2026
（82 筆 amount/labor/health/pension 級距金額）搬到 insurance_brackets 表。
同時為 insurance_rates 加三制度上限欄位（勞保/健保/勞退），讓每年公告
新上限時可純 DB 改動。

Why: 政府每年/每數年公告新的投保金額分級表與上限，原本必須改 .py 檔
+ 部署。改成 DB 後，園所行政可在 UI 維護，且歷史月份保留當時級距。

Revision ID: d9e0f1g2h3i4
Revises: c8d9e0f1g2h3
Create Date: 2026-05-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "d9e0f1g2h3i4"
down_revision = "c8d9e0f1g2h3"
branch_labels = None
depends_on = None


# 2026 (民國 115) 級距資料 — 來源：原 services/insurance_service.py:INSURANCE_TABLE_2026
# 之後每年新公告，行政透過 UI/API 新增 effective_year=新年度 的列即可。
_BRACKETS_2026 = [
    (1500, 277, 972, 458, 1428, 90),
    (3000, 277, 972, 458, 1428, 180),
    (4500, 277, 972, 458, 1428, 270),
    (6000, 277, 972, 458, 1428, 360),
    (7500, 277, 972, 458, 1428, 450),
    (8700, 277, 972, 458, 1428, 522),
    (9900, 277, 972, 458, 1428, 594),
    (11100, 277, 972, 458, 1428, 666),
    (12540, 313, 1097, 458, 1428, 752),
    (13500, 338, 1182, 458, 1428, 810),
    (15840, 396, 1386, 458, 1428, 950),
    (16500, 413, 1444, 458, 1428, 990),
    (17280, 432, 1512, 458, 1428, 1037),
    (17880, 447, 1564, 458, 1428, 1073),
    (19047, 476, 1666, 458, 1428, 1143),
    (20008, 500, 1751, 458, 1428, 1200),
    (21009, 525, 1838, 458, 1428, 1261),
    (22000, 550, 1925, 458, 1428, 1320),
    (23100, 577, 2022, 458, 1428, 1386),
    (24000, 600, 2100, 458, 1428, 1440),
    (25250, 632, 2210, 458, 1428, 1515),
    (26400, 660, 2310, 458, 1428, 1584),
    (27600, 690, 2415, 458, 1428, 1656),
    (28590, 715, 2501, 458, 1428, 1715),
    (29500, 738, 2582, 458, 1428, 1770),
    (30300, 758, 2651, 470, 1466, 1818),
    (31800, 795, 2783, 493, 1539, 1908),
    (33300, 833, 2914, 516, 1611, 1998),
    (34800, 870, 3045, 540, 1684, 2088),
    (36300, 908, 3176, 563, 1757, 2178),
    (38200, 955, 3342, 592, 1849, 2292),
    (40100, 1002, 3509, 622, 1940, 2406),
    (42000, 1050, 3675, 651, 2032, 2520),
    (43900, 1098, 3841, 681, 2124, 2634),
    (45800, 1145, 4008, 710, 2216, 2748),
    (48200, 1145, 4008, 748, 2332, 2892),
    (50600, 1145, 4008, 785, 2449, 3036),
    (53000, 1145, 4008, 822, 2565, 3180),
    (55400, 1145, 4008, 859, 2681, 3324),
    (57800, 1145, 4008, 896, 2797, 3468),
    (60800, 1145, 4008, 943, 2942, 3648),
    (63800, 1145, 4008, 990, 3087, 3828),
    (66800, 1145, 4008, 1036, 3233, 4008),
    (69800, 1145, 4008, 1083, 3378, 4188),
    (72800, 1145, 4008, 1129, 3523, 4368),
    (76500, 1145, 4008, 1187, 3702, 4590),
    (80200, 1145, 4008, 1244, 3881, 4812),
    (83900, 1145, 4008, 1301, 4060, 5034),
    (87600, 1145, 4008, 1359, 4239, 5256),
    (92100, 1145, 4008, 1428, 4457, 5526),
    (96600, 1145, 4008, 1498, 4675, 5796),
    (101100, 1145, 4008, 1568, 4892, 6066),
    (105600, 1145, 4008, 1638, 5110, 6336),
    (110100, 1145, 4008, 1708, 5328, 6606),
    (115500, 1145, 4008, 1791, 5589, 6930),
    (120900, 1145, 4008, 1875, 5850, 7254),
    (126300, 1145, 4008, 1959, 6112, 7578),
    (131700, 1145, 4008, 2043, 6373, 7902),
    (137100, 1145, 4008, 2126, 6634, 8226),
    (142500, 1145, 4008, 2210, 6896, 8550),
    (147900, 1145, 4008, 2294, 7157, 8874),
    (150000, 1145, 4008, 2327, 7259, 9000),
    (156400, 1145, 4008, 2426, 7568, 9000),
    (162800, 1145, 4008, 2525, 7878, 9000),
    (169200, 1145, 4008, 2624, 8188, 9000),
    (175600, 1145, 4008, 2724, 8497, 9000),
    (182000, 1145, 4008, 2823, 8807, 9000),
    (189500, 1145, 4008, 2939, 9170, 9000),
    (197000, 1145, 4008, 3055, 9533, 9000),
    (204500, 1145, 4008, 3172, 9896, 9000),
    (212000, 1145, 4008, 3288, 10259, 9000),
    (219500, 1145, 4008, 3404, 10622, 9000),
    (228200, 1145, 4008, 3539, 11043, 9000),
    (236900, 1145, 4008, 3674, 11464, 9000),
    (245600, 1145, 4008, 3809, 11885, 9000),
    (254300, 1145, 4008, 3944, 12306, 9000),
    (263000, 1145, 4008, 4079, 12727, 9000),
    (273000, 1145, 4008, 4234, 13211, 9000),
    (283000, 1145, 4008, 4389, 13695, 9000),
    (293000, 1145, 4008, 4544, 14179, 9000),
    (303000, 1145, 4008, 4700, 14663, 9000),
    (313000, 1145, 4008, 4855, 15146, 9000),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # 1. 建 insurance_brackets 表
    if "insurance_brackets" not in inspector.get_table_names():
        op.create_table(
            "insurance_brackets",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "effective_year",
                sa.Integer(),
                nullable=False,
                comment="適用年度（西元，與 InsuranceRate.rate_year 對齊）",
            ),
            sa.Column("amount", sa.Integer(), nullable=False, comment="投保金額"),
            sa.Column(
                "labor_employee", sa.Integer(), nullable=False, comment="勞保員工自付"
            ),
            sa.Column(
                "labor_employer", sa.Integer(), nullable=False, comment="勞保雇主負擔"
            ),
            sa.Column(
                "health_employee",
                sa.Integer(),
                nullable=False,
                comment="健保員工自付（單口）",
            ),
            sa.Column(
                "health_employer",
                sa.Integer(),
                nullable=False,
                comment="健保雇主負擔",
            ),
            sa.Column(
                "pension",
                sa.Integer(),
                nullable=False,
                comment="勞退雇主提繳（6%，等同提繳工資 × 0.06）",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "effective_year", "amount", name="uq_bracket_year_amount"
            ),
        )
        op.create_index(
            "ix_bracket_year_amount",
            "insurance_brackets",
            ["effective_year", "amount"],
        )

    # 2. 為 insurance_rates 加三制度上限欄位
    if "insurance_rates" in inspector.get_table_names():
        rate_cols = [c["name"] for c in inspector.get_columns("insurance_rates")]
        if "labor_max_insured" not in rate_cols:
            op.add_column(
                "insurance_rates",
                sa.Column(
                    "labor_max_insured",
                    sa.Integer(),
                    nullable=True,
                    comment="勞保（含就保）最高月投保薪資；NULL=沿用程式預設",
                ),
            )
        if "health_max_insured" not in rate_cols:
            op.add_column(
                "insurance_rates",
                sa.Column(
                    "health_max_insured",
                    sa.Integer(),
                    nullable=True,
                    comment="健保最高月投保金額；NULL=沿用程式預設",
                ),
            )
        if "pension_max_insured" not in rate_cols:
            op.add_column(
                "insurance_rates",
                sa.Column(
                    "pension_max_insured",
                    sa.Integer(),
                    nullable=True,
                    comment="勞退最高月提繳工資；NULL=沿用程式預設",
                ),
            )

        # 回填現有 InsuranceRate（rate_year=2026 預設值）
        op.execute("""
            UPDATE insurance_rates
            SET labor_max_insured = 45800,
                health_max_insured = 219500,
                pension_max_insured = 150000
            WHERE rate_year = 2026
              AND (labor_max_insured IS NULL
                   OR health_max_insured IS NULL
                   OR pension_max_insured IS NULL)
            """)

    # 3. 寫入 2026 級距資料（先檢查是否已有同 (year, amount) 的列，避免重跑炸 unique 約束）
    if "insurance_brackets" in inspector.get_table_names():
        existing = {
            (row[0], row[1])
            for row in bind.execute(
                sa.text(
                    "SELECT effective_year, amount FROM insurance_brackets "
                    "WHERE effective_year = 2026"
                )
            ).fetchall()
        }
        rows_to_insert = [
            {
                "effective_year": 2026,
                "amount": amount,
                "labor_employee": le,
                "labor_employer": lr,
                "health_employee": he,
                "health_employer": hr,
                "pension": p,
            }
            for amount, le, lr, he, hr, p in _BRACKETS_2026
            if (2026, amount) not in existing
        ]
        if rows_to_insert:
            brackets_table = sa.table(
                "insurance_brackets",
                sa.column("effective_year", sa.Integer),
                sa.column("amount", sa.Integer),
                sa.column("labor_employee", sa.Integer),
                sa.column("labor_employer", sa.Integer),
                sa.column("health_employee", sa.Integer),
                sa.column("health_employer", sa.Integer),
                sa.column("pension", sa.Integer),
            )
            op.bulk_insert(brackets_table, rows_to_insert)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # 移除 insurance_rates 三個 max 欄位
    if "insurance_rates" in inspector.get_table_names():
        rate_cols = [c["name"] for c in inspector.get_columns("insurance_rates")]
        for col in ("labor_max_insured", "health_max_insured", "pension_max_insured"):
            if col in rate_cols:
                op.drop_column("insurance_rates", col)

    # 刪 insurance_brackets 表
    if "insurance_brackets" in inspector.get_table_names():
        op.drop_index("ix_bracket_year_amount", table_name="insurance_brackets")
        op.drop_table("insurance_brackets")
