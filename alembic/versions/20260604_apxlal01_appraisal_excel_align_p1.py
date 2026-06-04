"""appraisal P1：對齊 Excel 獎金 3 組 + scoring rules 覆蓋 114學年上 + SPED。

解 effective-date silent-0 bug：
  - appraisal_bonus_rates  只有 effective_from='2026-08-01'
    → 114上（base_score_calc_date≈2025-09-15）和 114下（2026-03-15）查不到 rate
    → compute_bonus_amount 回傳 0（silent，無 warning 可見）
  - appraisal_scoring_rules 只有 effective_from='2026-01-01'
    → 114上（2025-09-15）查不到 rule → 使用 fallback 空集合

修正：在 effective_from='2025-08-01' 插入對齊 Excel 的獎金率 + 完整 15 條 rules。
同時更新既有 '2026-08-01' 行為對齊值（115學年也採 Excel 值，業主已確認）。

downgrade 還原：刪 2025-08-01 行 + 還原 2026-08-01 被異動的 3 組原值。

Revision ID: apxlal01
Revises: acadterm01
Create Date: 2026-06-04
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "apxlal01"
down_revision = "acadterm01"
branch_labels = None
depends_on = None

ALIGN_EFFECTIVE = "2025-08-01"  # 114學年上起算（民國114年8月1日）
RULES_EFFECTIVE = "2025-08-01"
EXISTING_RATE_EFFECTIVE = "2026-08-01"  # 既有 seed，需更新為對齊值

# 對齊 Excel 3 組（SUPERVISOR/HEAD_TEACHER 不變，ASSISTANT/STAFF/COOK 對齊）
# downgrade 只還原有變動的 3 組：ASSISTANT/STAFF/COOK
ALIGNED_RATES = [
    ("SUPERVISOR", "OUTSTANDING", 8000),
    ("SUPERVISOR", "GOOD", 5000),
    ("HEAD_TEACHER", "OUTSTANDING", 6000),
    ("HEAD_TEACHER", "GOOD", 4000),
    ("ASSISTANT", "OUTSTANDING", 5500),
    ("ASSISTANT", "GOOD", 3500),
    ("STAFF", "OUTSTANDING", 6000),
    ("STAFF", "GOOD", 4000),
    ("COOK", "OUTSTANDING", 6000),
    ("COOK", "GOOD", 4000),
]

# 原始 seed 值（僅 3 組有異動，用於 downgrade 還原）
_ORIGINAL_2026_08_01 = [
    ("ASSISTANT", "OUTSTANDING", 4500),
    ("ASSISTANT", "GOOD", 3000),
    ("STAFF", "OUTSTANDING", 5000),
    ("STAFF", "GOOD", 3500),
    ("COOK", "OUTSTANDING", 3500),
    ("COOK", "GOOD", 2500),
]

# 14 條原有 rules（byte-for-byte 對齊 aprcal001 DEFAULT_RULES）+ SPED
# 讓 114上（effective_from=2025-08-01）與 114下（2026-01-01）使用相同計算邏輯
_TEACHING = ["HEAD_TEACHER", "ASSISTANT"]

RULES = [
    ("LATE_EARLY", "PER_UNIT", {"per_unit_delta": -0.25}, None),
    ("MISSING_PUNCH", "PER_UNIT", {"per_unit_delta": -0.25}, None),
    ("LEAVE", "PER_UNIT", {"per_unit_delta": -1.0}, None),
    (
        "RETURNING_RATE_0915",
        "TIER",
        {
            "input_field": "retention_rate",
            "tiers": [
                {"min": 100, "delta": 0},
                {"min": 95, "delta": -1.7},
                {"min": 0, "delta": -3.0},
            ],
        },
        _TEACHING,
    ),
    (
        "RETURNING_RATE_0315",
        "TIER",
        {
            "input_field": "retention_rate",
            "tiers": [
                {"min": 100, "delta": 6.0},
                {"min": 95, "delta": 0.0},
                {"min": 90, "delta": -1.7},
                {"min": 80, "delta": -3.0},
                {"min": 0, "delta": -6.0},
            ],
        },
        _TEACHING,
    ),
    (
        "AFTER_CLASS_RATE",
        "FLAT_THRESHOLD",
        {
            "input_field": "activity_rate",
            "threshold": 80,
            "above_delta": 2.0,
            "below_delta": 0,
        },
        _TEACHING,
    ),
    (
        "REWARD_PUNISH",
        "DISCIPLINARY_TIERED",
        {
            "warning_delta": -1.0,
            "minor_delta": -3.0,
            "major_delta": -10.0,
        },
        None,
    ),
    ("SCHOOL_MEETING_ABSENCE", "PER_UNIT", {"per_unit_delta": -1.0}, None),
    ("INSTITUTION_MEETING_0913", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("INSTITUTION_MEETING_1115", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("SELF_IMPROVEMENT_ACTIVITY", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("CHILD_ACCIDENT", "PER_UNIT", {"per_unit_delta": -3.0}, None),
    ("CLASS_HEADCOUNT_BONUS", "PER_UNIT", {"per_unit_delta": 2.0}, None),
    ("SPED", "PER_UNIT", {"per_unit_delta": 2.0}, None),  # 特教生 +2/位（Task 2）
    ("OTHER", "PER_UNIT", {"per_unit_delta": 0}, None),
]


def _has_table(bind: sa.engine.Connection, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    # ① bonus rates：插 2025-08-01 對齊值（冪等：跳過已存在的 (eff, rg, grade)）
    # role_group/grade 為 PG enum 欄位，依賴 psycopg2 的 unknown-OID text coercion（
    # 與 20260511_a3p4p5r6i7s8_appraisal_seed_bonus_rates.py 慣例相同；不加 CAST 以
    # 避免硬編 enum 型別名稱）
    if _has_table(bind, "appraisal_bonus_rates"):
        existing = {
            (
                r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
                r[1],
                r[2],
            )
            for r in bind.execute(
                sa.text(
                    "SELECT effective_from, role_group, grade"
                    " FROM appraisal_bonus_rates"
                )
            ).fetchall()
        }
        for rg, gr, amt in ALIGNED_RATES:
            if (ALIGN_EFFECTIVE, rg, gr) not in existing:
                bind.execute(
                    sa.text(
                        "INSERT INTO appraisal_bonus_rates"
                        " (effective_from, role_group, grade, base_amount)"
                        " VALUES (:e, :rg, :gr, :a)"
                    ),
                    {"e": ALIGN_EFFECTIVE, "rg": rg, "gr": gr, "a": amt},
                )
        # 更新既有 2026-08-01 列為對齊值（115學年也採 Excel 值，業主已確認）
        # role_group/grade 同為 PG enum，同上依賴 psycopg2 unknown-OID text coercion；不加 CAST
        for rg, gr, amt in ALIGNED_RATES:
            bind.execute(
                sa.text(
                    "UPDATE appraisal_bonus_rates SET base_amount = :a"
                    " WHERE effective_from = :e AND role_group = :rg AND grade = :gr"
                ),
                {"a": amt, "e": EXISTING_RATE_EFFECTIVE, "rg": rg, "gr": gr},
            )

    # ② scoring rules：插 2025-08-01 全集（14 原有 + SPED = 15 條）
    #    冪等：跳過已存在的 (item_code, effective_from)
    if _has_table(bind, "appraisal_scoring_rules"):
        existing_rules = {
            (
                r[0],
                r[1].isoformat() if hasattr(r[1], "isoformat") else str(r[1]),
            )
            for r in bind.execute(
                sa.text(
                    "SELECT item_code, effective_from" " FROM appraisal_scoring_rules"
                )
            ).fetchall()
        }
        for code, rtype, cfg, roles in RULES:
            if (code, RULES_EFFECTIVE) in existing_rules:
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO appraisal_scoring_rules"
                    " (item_code, effective_from, rule_type, rule_config,"
                    "  applies_to_role_groups)"
                    " VALUES (:c, :e, :t,"
                    "  CAST(:cfg AS JSONB),"
                    "  CAST(:roles AS JSONB))"
                ),
                {
                    "c": code,
                    "e": RULES_EFFECTIVE,
                    "t": rtype,
                    "cfg": json.dumps(cfg),
                    "roles": json.dumps(roles) if roles is not None else None,
                },
            )


def downgrade() -> None:
    """還原本 migration 的 seed。

    注意：2026-08-01 的 3 組 base_amount 還原為 migration 前的原始 seed 值
    （非 upgrade 後 HR 在 UI 手動調整的值）——與既有 seed migration 慣例一致。
    """
    bind = op.get_bind()

    # ① 刪 2025-08-01 bonus rows
    if _has_table(bind, "appraisal_bonus_rates"):
        bind.execute(
            sa.text("DELETE FROM appraisal_bonus_rates WHERE effective_from = :e"),
            {"e": ALIGN_EFFECTIVE},
        )
        # 還原 2026-08-01 中有異動的 3 組回原始值
        for rg, gr, amt in _ORIGINAL_2026_08_01:
            bind.execute(
                sa.text(
                    "UPDATE appraisal_bonus_rates SET base_amount = :a"
                    " WHERE effective_from = :e AND role_group = :rg AND grade = :gr"
                ),
                {"a": amt, "e": EXISTING_RATE_EFFECTIVE, "rg": rg, "gr": gr},
            )

    # ② 刪 2025-08-01 scoring rule rows
    if _has_table(bind, "appraisal_scoring_rules"):
        bind.execute(
            sa.text("DELETE FROM appraisal_scoring_rules WHERE effective_from = :e"),
            {"e": RULES_EFFECTIVE},
        )
