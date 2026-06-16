"""回歸測試：grade_targets 欄位為 NULL 時節慶/超額獎金不應靜默歸 0。

Bug #14：
- 資料來源 engine.py 將 GradeTarget 的 festival_two_teachers/one_teacher/shared
  與 overtime_* 直接賦值進 target map；若該欄在 DB 為 NULL，map 內就是 None。
- festival.get_target_enrollment / get_overtime_target 用 ``targets.get(key, 0)``
  取值，但 key 存在且值為 None 時 ``.get`` 回傳 None（不是 default 0）。
- calculate_festival_bonus_v2 的 ``if target > 0`` 對 None 拋 TypeError，
  calculate_overtime_bonus 的 ``current_enrollment - overtime_target`` 同樣會炸；
  此例外被引擎廣域 except 吞掉 → 該年級全體帶班老師節慶/超額獎金靜默歸 0（少發）。

修法（純程式碼，無 migration）：
① engine 賦值時每欄套 ``or 0``（防止 None 進入 map）。
② festival.get_target_enrollment / get_overtime_target 在 ``targets.get(key, 0)``
   後加 ``or 0`` 正規化 None→0（防禦縱深，即使 map 仍帶 None 也不炸）。

本檔為純函式測試，不觸發任何 DB 寫入。
"""

import pytest

from services.salary.festival import (
    calculate_festival_bonus_v2,
    get_overtime_target,
    get_target_enrollment,
)

# 模擬 GradeTarget 欄位全為 NULL（DB 未填）時，engine 賦值產生的 map
_NULL_TARGET_MAP = {
    "大班": {
        "2_teachers": None,
        "1_teacher": None,
        "shared_assistant": None,
    }
}
_NULL_OVERTIME_MAP = {
    "大班": {
        "2_teachers": None,
        "1_teacher": None,
        "shared_assistant": None,
    }
}

# art_teacher 在 FESTIVAL_BONUS_BASE 有獨立基數 2000，這裡只需提供能取到基數的結構
_BONUS_BASE = {
    "head_teacher": {"幼兒園教師": 2000},
    "assistant_teacher": {"教保員": 1500},
    "art_teacher": 2000,
}
_OVERTIME_PER_PERSON = {
    "head_teacher": {"大班": 100},
    "assistant_teacher": {"大班": 50},
}


class TestGetTargetEnrollmentNull:
    @pytest.mark.parametrize(
        "has_assistant,is_shared",
        [(True, False), (False, False), (False, True)],
    )
    def test_returns_zero_when_target_value_is_none(self, has_assistant, is_shared):
        """欄位為 NULL（map 值 None）時應正規化回 0，而非 None。"""
        result = get_target_enrollment(
            grade_name="大班",
            has_assistant=has_assistant,
            is_shared_assistant=is_shared,
            target_map=_NULL_TARGET_MAP,
        )
        assert result == 0
        assert result is not None

    @pytest.mark.parametrize(
        "has_assistant,is_shared",
        [(True, False), (False, False), (False, True)],
    )
    def test_overtime_target_returns_zero_when_none(self, has_assistant, is_shared):
        result = get_overtime_target(
            grade_name="大班",
            has_assistant=has_assistant,
            is_shared_assistant=is_shared,
            target_map=_NULL_OVERTIME_MAP,
        )
        assert result == 0
        assert result is not None


class TestCalculateFestivalBonusV2NullTarget:
    def test_does_not_raise_typeerror_on_null_target(self):
        """grade_target 欄位為 NULL 時不應拋 TypeError（否則被引擎吞掉→靜默歸 0）。"""
        result = calculate_festival_bonus_v2(
            position="幼兒園教師",
            role="head_teacher",
            grade_name="大班",
            current_enrollment=25,
            has_assistant=True,
            is_shared_assistant=False,
            bonus_base=_BONUS_BASE,
            target_enrollment_map=_NULL_TARGET_MAP,
            overtime_target_map=_NULL_OVERTIME_MAP,
            overtime_per_person_map=_OVERTIME_PER_PERSON,
        )
        # target=0 → ratio=0, festival_bonus=0（by design：目標未設定不發節慶）
        # 但 overtime_target=0 時，超額人數 = current_enrollment - 0 = 全員超額
        assert result["target"] == 0
        assert result["festival_bonus"] == 0
        assert result["overtime_target"] == 0
        # current_enrollment 25 - overtime_target 0 = 25 人超額，每人 100 → 2500
        assert result["overtime_count"] == 25
        assert result["overtime_bonus"] == 2500
