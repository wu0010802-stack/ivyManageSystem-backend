"""tests/test_appraisal_regulation_align.py — 規章第六篇對齊（spec 2026-06-11）"""

from decimal import Decimal

from models.appraisal import AUTO_ITEM_CODES, MANUAL_ITEM_CODES, ScoreItemCode

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
