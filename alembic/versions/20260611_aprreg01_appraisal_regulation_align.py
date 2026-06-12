"""考核對齊人事規章第六篇（spec 2026-06-11）。

① appraisal_bonus_rates：五組值改為規章值（兩個 effective set 都改，in-place）：
   SUPERVISOR 優 8000→10000、HEAD_TEACHER 優 6000→8000、STAFF 優 6000→8000、
   ASSISTANT 優 5500→6000、COOK 甲 4000→3500。
   （114上 僅出現 HEAD_TEACHER 甲等案例，本次改值不影響任何歷史金額。）
② appraisal_scoring_rules：插入 effective_from='2026-02-01'（114下起）全套 24 條，
   既有 2025-08-01 列不動（歷史學期重算結果不變）。

Revision ID: aprreg01
Revises: yebnd01
Create Date: 2026-06-11

⚠ merge 進 main 前須 re-parent down_revision 至當時 main head（撰寫時 main 為 recvisuq01；
   前例見 auditack01/recvisuq01 的 reparent 註記），merge 後跑 alembic heads 驗單一 head。
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "aprreg01"
down_revision = "yebnd01"
branch_labels = None
depends_on = None

RATE_EFFECTIVES = ("2025-08-01", "2026-08-01")

# (role_group, grade, 規章值, 還原值)
RATE_CHANGES = [
    ("SUPERVISOR", "OUTSTANDING", 10000, 8000),
    ("HEAD_TEACHER", "OUTSTANDING", 8000, 6000),
    ("STAFF", "OUTSTANDING", 8000, 6000),
    ("ASSISTANT", "OUTSTANDING", 6000, 5500),
    ("COOK", "GOOD", 3500, 4000),
]

RULES_EFFECTIVE = "2026-02-01"  # 114下（2026-02-01~07-31）起適用
_TEACHING = ["HEAD_TEACHER", "ASSISTANT"]

# 24 條：15 既有 code（規章新值或照抄）＋ 9 新 code
RULES = [
    # --- 出缺勤（第五條(二)）---
    ("LATE_EARLY", "PER_UNIT", {"per_unit_delta": -0.25}, None),
    ("MISSING_PUNCH", "PER_UNIT", {"per_unit_delta": -0.25}, None),
    ("LEAVE", "PER_UNIT", {"per_unit_delta": -1.0}, None),
    ("ABSENTEEISM", "PER_UNIT", {"per_unit_delta": -4.0}, None),
    # --- 留校率（第五條(七)；未帶班吃全校平均 → applies None）---
    (
        "RETURNING_RATE_0915",
        "TIER",
        {
            "input_field": "retention_rate",
            "tiers": [
                {"min": 100, "delta": 6.0},
                {"min": 95, "delta": 0.0},
                {"min": 90, "delta": -2.0},
                {"min": 80, "delta": -3.0},
                {"min": 0, "delta": -4.0},
            ],
        },
        None,
    ),
    (
        "RETURNING_RATE_0315",
        "TIER",
        {
            "input_field": "retention_rate",
            "tiers": [
                {"min": 100, "delta": 6.0},
                {"min": 95, "delta": 0.0},
                {"min": 90, "delta": -2.0},
                {"min": 80, "delta": -3.0},
                {"min": 0, "delta": -4.0},
            ],
        },
        None,
    ),
    # --- 才藝班參加率（第五條(九)：分年級門檻）---
    (
        "AFTER_CLASS_RATE",
        "FLAT_THRESHOLD",
        {
            "input_field": "activity_rate",
            "threshold": 80,
            "above_delta": 2.0,
            "below_delta": 0,
            "grade_thresholds": {"大班": 100, "中班": 90, "小班": 80, "幼幼班": 70},
        },
        _TEACHING,
    ),
    # --- 獎懲（第五條(十)：功過相抵）---
    (
        "REWARD_PUNISH",
        "DISCIPLINARY_TIERED",
        {
            "warning_delta": -2.0,
            "minor_delta": -3.0,
            "major_delta": -6.0,
            "commend_delta": 2.0,
            "minor_merit_delta": 3.0,
            "major_merit_delta": 6.0,
        },
        None,
    ),
    # --- 會議活動（第五條(十二)：每時數 −0.5；count=計分時數，每次活動最多計 4 小時=封頂 −2 由填報執行）---
    ("SCHOOL_MEETING_ABSENCE", "PER_UNIT", {"per_unit_delta": -0.5}, None),
    ("INSTITUTION_MEETING_0913", "PER_UNIT", {"per_unit_delta": -0.5}, None),
    ("INSTITUTION_MEETING_1115", "PER_UNIT", {"per_unit_delta": -0.5}, None),
    ("SELF_IMPROVEMENT_ACTIVITY", "PER_UNIT", {"per_unit_delta": -0.5}, None),
    # --- 幼兒意外（第五條(六)：主管評議 1~10 分）---
    ("CHILD_ACCIDENT", "MANUAL_DELTA", {"min_delta": -10, "max_delta": 0}, None),
    # --- 帶班/特教（不變值，新版本照抄）---
    ("CLASS_HEADCOUNT_BONUS", "PER_UNIT", {"per_unit_delta": 2.0}, None),
    ("SPED", "PER_UNIT", {"per_unit_delta": 2.0}, None),
    ("OTHER", "PER_UNIT", {"per_unit_delta": 0}, None),
    # --- 休學細則（第五條(五)）---
    ("STUDENT_WITHDRAWAL", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("STUDENT_REINSTATE", "PER_UNIT", {"per_unit_delta": 1.0}, None),
    ("TRIAL_LEAVE", "PER_UNIT", {"per_unit_delta": -1.0}, None),
    ("CLASS_TRANSFER", "PER_UNIT", {"per_unit_delta": -0.5}, None),
    # --- 公告制手填分值 ---
    ("EXAM_RESULT", "MANUAL_DELTA", {"min_delta": -10, "max_delta": 10}, None),
    ("RECRUIT_SCORE", "MANUAL_DELTA", {"min_delta": 0, "max_delta": 20}, None),
    ("SUPERVISOR_SCORE", "MANUAL_DELTA", {"min_delta": 0, "max_delta": 10}, None),
    # --- 呈報優異（第五條(十一)1：每學期 1 位 → unit_cap=1）---
    ("EXCELLENCE_NOMINATION", "PER_UNIT", {"per_unit_delta": 2.0, "unit_cap": 1}, None),
]


def _has_table(bind: sa.engine.Connection, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "appraisal_bonus_rates"):
        for eff in RATE_EFFECTIVES:
            for rg, gr, new_amt, _old in RATE_CHANGES:
                result = bind.execute(
                    sa.text(
                        "UPDATE appraisal_bonus_rates SET base_amount = :a"
                        " WHERE effective_from = :e AND role_group = :rg AND grade = :gr"
                    ),
                    {"a": new_amt, "e": eff, "rg": rg, "gr": gr},
                )
                if eff == "2025-08-01" and result.rowcount == 0:
                    print(
                        f"WARNING aprreg01: bonus_rate ({rg},{gr}) eff={eff} 不存在，"
                        "對齊未生效——檢查該環境 seed 是否漂移"
                    )

    if _has_table(bind, "appraisal_scoring_rules"):
        existing = {
            (r[0], r[1].isoformat() if hasattr(r[1], "isoformat") else str(r[1]))
            for r in bind.execute(
                sa.text("SELECT item_code, effective_from FROM appraisal_scoring_rules")
            ).fetchall()
        }
        for code, rtype, cfg, roles in RULES:
            if (code, RULES_EFFECTIVE) in existing:
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO appraisal_scoring_rules"
                    " (item_code, effective_from, rule_type, rule_config,"
                    "  applies_to_role_groups)"
                    " VALUES (:c, :e, :t, CAST(:cfg AS JSONB), CAST(:roles AS JSONB))"
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
    """還原獎金率五組 seed 值（非 HR 後續手調值）＋刪 2026-02-01 規則列。"""
    bind = op.get_bind()
    if _has_table(bind, "appraisal_bonus_rates"):
        for eff in RATE_EFFECTIVES:
            for rg, gr, _new, old_amt in RATE_CHANGES:
                bind.execute(
                    sa.text(
                        "UPDATE appraisal_bonus_rates SET base_amount = :a"
                        " WHERE effective_from = :e AND role_group = :rg AND grade = :gr"
                    ),
                    {"a": old_amt, "e": eff, "rg": rg, "gr": gr},
                )
    if _has_table(bind, "appraisal_scoring_rules"):
        codes = [c for c, _t, _cfg, _r in RULES]
        bind.execute(
            sa.text(
                "DELETE FROM appraisal_scoring_rules"
                " WHERE effective_from = :e AND item_code = ANY(:codes)"
            ),
            {"e": RULES_EFFECTIVE, "codes": codes},
        )
