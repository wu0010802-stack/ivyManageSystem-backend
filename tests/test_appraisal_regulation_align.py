"""tests/test_appraisal_regulation_align.py — 規章第六篇對齊（spec 2026-06-11）"""

from datetime import date
from decimal import Decimal

from models.appraisal import (
    AUTO_ITEM_CODES,
    MANUAL_ITEM_CODES,
    RoleGroup,
    ScoreItemCode,
)
from services.appraisal.rule_applier import ScoringRule, apply_manual_delta

NEW_CODES = {
    "ABSENTEEISM",
    "STUDENT_WITHDRAWAL",
    "STUDENT_REINSTATE",
    "TRIAL_LEAVE",
    "CLASS_TRANSFER",
    "EXAM_RESULT",
    "RECRUIT_SCORE",
    "SUPERVISOR_SCORE",
    "EXCELLENCE_NOMINATION",
}


def test_score_item_code_新增九項():
    assert NEW_CODES <= {c.value for c in ScoreItemCode}


def test_auto_manual_歸類():
    assert ScoreItemCode.ABSENTEEISM in AUTO_ITEM_CODES
    assert ScoreItemCode.STUDENT_REINSTATE in AUTO_ITEM_CODES
    # 休學降級手填（spec §14.3）；其餘新項皆手填
    for code in (
        "STUDENT_WITHDRAWAL",
        "TRIAL_LEAVE",
        "CLASS_TRANSFER",
        "EXAM_RESULT",
        "RECRUIT_SCORE",
        "SUPERVISOR_SCORE",
        "EXCELLENCE_NOMINATION",
    ):
        assert ScoreItemCode(code) in MANUAL_ITEM_CODES


# ===== Task 3: MANUAL_DELTA 規則型別 =====


def _md_rule(lo, hi):
    return ScoringRule(
        item_code="CHILD_ACCIDENT",
        effective_from=date(2026, 2, 1),
        rule_type="MANUAL_DELTA",
        rule_config={"min_delta": lo, "max_delta": hi},
        applies_to_role_groups=None,
    )


def test_manual_delta_範圍內原值():
    rule = _md_rule(-10, 0)
    assert apply_manual_delta(rule, Decimal("-3.5"), RoleGroup.HEAD_TEACHER) == Decimal(
        "-3.50"
    )


def test_manual_delta_下限clamp():
    rule = _md_rule(-10, 0)
    assert apply_manual_delta(rule, Decimal("-15"), RoleGroup.HEAD_TEACHER) == Decimal(
        "-10.00"
    )


def test_manual_delta_上限clamp():
    rule = _md_rule(0, 20)
    assert apply_manual_delta(rule, Decimal("25"), RoleGroup.HEAD_TEACHER) == Decimal(
        "20.00"
    )


def test_manual_delta_邊界值不截斷():
    rule = _md_rule(-10, 0)
    assert apply_manual_delta(rule, Decimal("-10"), RoleGroup.HEAD_TEACHER) == Decimal(
        "-10.00"
    )
    assert apply_manual_delta(rule, Decimal("0"), RoleGroup.HEAD_TEACHER) == Decimal(
        "0.00"
    )
