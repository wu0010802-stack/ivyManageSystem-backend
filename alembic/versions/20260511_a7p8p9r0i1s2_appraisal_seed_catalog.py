"""appraisal: seed 15 筆 score_item_catalog（M1 重構）

對應 Excel「114(上)年度考核統計表」16 欄位中編號 2-16（編號 1「9/15 分數」即基礎分數，
存於 appraisal_cycles.base_score，不屬於 score_items）。

display_order 對齊 Excel 欄位順序；data_source 註記是否可從其他模組自動帶入。

Revision ID: a7p8p9r0i1s2
Revises: a1p2p3r4i5s6
Create Date: 2026-05-11 (rewritten 2026-05-15 for M1)
"""

import sqlalchemy as sa
from alembic import op

revision = "a7p8p9r0i1s2"
down_revision = "a1p2p3r4i5s6"
branch_labels = None
depends_on = None


# (code, label, sign, default_weight, data_source, description, display_order)
CATALOG_ITEMS = [
    (
        "LEAVE",
        "請休假",
        "NEGATIVE",
        0,
        "leave",
        "請假與休假合併扣分；公式於 engine 計算",
        1,
    ),
    (
        "LATE_EARLY",
        "遲到/早退",
        "NEGATIVE",
        -0.25,
        "attendance",
        "每次 -0.25；可從 attendance 模組自動匯入",
        2,
    ),
    (
        "NO_CLOCK",
        "未打卡",
        "NEGATIVE",
        -0.25,
        "attendance",
        "每次 -0.25",
        3,
    ),
    (
        "MISS_PRESCHOOL_MEETING",
        "園務會議未參加",
        "NEGATIVE",
        -1,
        "manual",
        "每次 -1；由主管手動登錄",
        4,
    ),
    (
        "ORG_MEETING_0913",
        "9/13 機構會議研習",
        "NEGATIVE",
        -2,
        "manual",
        "未參加扣 -2",
        5,
    ),
    (
        "ORG_MEETING_1115",
        "11/15 機構會議研習",
        "NEGATIVE",
        -2,
        "manual",
        "未參加扣 -2",
        6,
    ),
    (
        "TEAM_ACTIVITY_1115",
        "11/15 自強活動",
        "NEGATIVE",
        -2,
        "manual",
        "未參加扣 -2",
        7,
    ),
    (
        "DROPOUT_0915",
        "9/15 休學人數",
        "NEGATIVE",
        0,
        "manual",
        "休學人數扣分 = 休學人數×係數，公式於 engine 計算",
        8,
    ),
    (
        "DROPOUT_0315",
        "3/15 休學人數",
        "NEGATIVE",
        0,
        "manual",
        "公式：(全園休學×2 + 試讀休學×1 - 回園×1)/班級數",
        9,
    ),
    (
        "CHILD_INCIDENT",
        "幼兒意外",
        "NEGATIVE",
        0,
        "manual",
        "依嚴重度扣分；note 存事件明細",
        10,
    ),
    (
        "RETURNING_RATE_0315",
        "3/15 舊生註冊率",
        "NEUTRAL",
        0,
        "monthly_enrollment_snapshots",
        "舊生註冊率達標加分、未達扣分",
        11,
    ),
    (
        "CLASS_SIZE",
        "帶班人數",
        "NEUTRAL",
        0,
        "monthly_enrollment_snapshots",
        "編制以上加、以下扣；公式於 engine 計算",
        12,
    ),
    (
        "AFTER_CLASS_RATE",
        "才藝班參加率",
        "POSITIVE",
        0,
        "activity_service",
        "達 100% 加 2 分；可從 activity 模組自動帶入",
        13,
    ),
    (
        "SPED",
        "特別辦法（特教生）",
        "POSITIVE",
        2,
        "manual",
        "每位特教生 +2",
        14,
    ),
    (
        "REWARD_PUNISH",
        "獎懲",
        "NEUTRAL",
        0,
        "disciplinary",
        "可多筆並列；note 存大過/嘉獎明細",
        15,
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "appraisal_score_item_catalog" not in inspector.get_table_names():
        return

    catalog_table = sa.table(
        "appraisal_score_item_catalog",
        sa.column("code", sa.String),
        sa.column("label", sa.String),
        sa.column("sign", sa.String),
        sa.column("default_weight", sa.Numeric),
        sa.column("data_source", sa.String),
        sa.column("description", sa.Text),
        sa.column("display_order", sa.Integer),
        sa.column("is_active", sa.Boolean),
    )

    existing_codes = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT code FROM appraisal_score_item_catalog")
        ).fetchall()
    }
    rows_to_insert = []
    for code, label, sign, weight, data_source, desc, order in CATALOG_ITEMS:
        if code in existing_codes:
            continue
        rows_to_insert.append(
            {
                "code": code,
                "label": label,
                "sign": sign,
                "default_weight": weight,
                "data_source": data_source,
                "description": desc,
                "display_order": order,
                "is_active": True,
            }
        )
    if rows_to_insert:
        op.bulk_insert(catalog_table, rows_to_insert)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "appraisal_score_item_catalog" not in inspector.get_table_names():
        return
    codes = [c[0] for c in CATALOG_ITEMS]
    bind.execute(
        sa.text(
            "DELETE FROM appraisal_score_item_catalog WHERE code = ANY(:codes)"
        ),
        {"codes": codes},
    )
