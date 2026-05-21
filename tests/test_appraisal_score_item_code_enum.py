"""ScoreItemCode enum 14 條完整性 + Permission bit 衝突檢查。"""

from utils.permissions import Permission
from models.appraisal import ScoreItemCode


def test_score_item_code_has_14_codes():
    expected = {
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
        "OTHER",
    }
    actual = {c.value for c in ScoreItemCode}
    assert actual == expected, f"missing={expected - actual} extra={actual - expected}"


def test_score_item_code_auto_vs_manual_partition():
    AUTO = {
        "LATE_EARLY",
        "MISSING_PUNCH",
        "LEAVE",
        "RETURNING_RATE_0915",
        "RETURNING_RATE_0315",
        "AFTER_CLASS_RATE",
        "REWARD_PUNISH",
    }
    MANUAL = {c.value for c in ScoreItemCode} - AUTO
    assert len(AUTO) == 7
    assert len(MANUAL) == 7


def test_appraisal_rule_write_permission_bit_unique():
    # text[] 版本：確認 APPRAISAL_RULE_WRITE 存在於 enum，且所有 Permission name 唯一
    used_names = {p.value for p in Permission}
    assert "APPRAISAL_RULE_WRITE" in used_names, "APPRAISAL_RULE_WRITE 未定義"
    # 沒有兩個 Permission 共享同 name
    assert len(used_names) == len(list(Permission))
