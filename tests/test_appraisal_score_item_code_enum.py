"""ScoreItemCode enum 完整性 + AUTO/MANUAL 分區 + Permission bit 衝突檢查。

原 15 條（14 calibrate + SPED）+ 規章第六篇對齊 9 條（aprreg01，
2026-02-01 起有規則）= 24 條。
"""

from utils.permissions import Permission
from models.appraisal import AUTO_ITEM_CODES, MANUAL_ITEM_CODES, ScoreItemCode


def test_score_item_code_has_24_codes():
    """規章第六篇對齊後 enum 共 24 項（15 舊 + 9 新）。"""
    expected = {
        # 原有 15 條
        "LATE_EARLY",
        "MISSING_PUNCH",
        "LEAVE",
        "RETURNING_RATE_0915",
        "RETURNING_RATE_0315",
        "AFTER_CLASS_RATE",
        "REWARD_PUNISH",
        "SCHOOL_MEETING_ABSENCE",
        "INSTITUTION_MEETING_0913",
        "INSTITUTION_MEETING_1115",
        "SELF_IMPROVEMENT_ACTIVITY",
        "CHILD_ACCIDENT",
        "CLASS_HEADCOUNT_BONUS",
        "SPED",
        "OTHER",
        # 規章第六篇新增 9 條（aprreg01；effective 2026-02-01）
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
    actual = {c.value for c in ScoreItemCode}
    assert actual == expected, f"missing={expected - actual} extra={actual - expected}"


def test_score_item_code_auto_vs_manual_partition():
    """AUTO 共 10 條（原 7 + CLASS_HEADCOUNT_BONUS + ABSENTEEISM + STUDENT_REINSTATE），
    MANUAL 共 14 條（其餘含 SPED 及 7 條規章新增手填項）。
    """
    AUTO = {c.value for c in AUTO_ITEM_CODES}
    MANUAL = {c.value for c in MANUAL_ITEM_CODES}
    assert len(AUTO) == 10
    assert len(MANUAL) == 14  # 24 total − 10 AUTO


def test_appraisal_rule_write_permission_bit_unique():
    # text[] 版本：確認 APPRAISAL_RULE_WRITE 存在於 enum，且所有 Permission name 唯一
    used_names = {p.value for p in Permission}
    assert "APPRAISAL_RULE_WRITE" in used_names, "APPRAISAL_RULE_WRITE 未定義"
    # 沒有兩個 Permission 共享同 name
    assert len(used_names) == len(list(Permission))
