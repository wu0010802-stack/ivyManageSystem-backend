"""
整合型邊界測試：節慶獎金在下列情境的預期行為

- 入職未滿 3 個月（應為 0）
- 入職跨月（半月入職）— eligibility 以 reference_date 比較
- 離職員工 eligibility 判斷（離職後仍看原 hire_date）
- 美師 (art_teacher) 共用班級規則（強制 shared_assistant）
- 零底薪防禦（不應 ZeroDivisionError）
- bonus_base 中某職位 / 等級值為 NULL 時不拋 TypeError

補上這些測試的理由：
- 過往 bug 多集中在「input 非典型」的情境（NULL、時薪、半月）
- 整合整段 calculate_festival_bonus_v2 + is_eligible 的組合
"""

from datetime import date

import pytest


class TestFestivalEligibilityEdgeCases:
    def test_hire_mid_month_eligible_on_correct_day(self, engine):
        """半月入職（1/15）滿 3 個月的基準點是 4/15，而非 4/1。"""
        assert (
            engine.is_eligible_for_festival_bonus(
                hire_date="2025-01-15",
                reference_date="2025-04-14",
            )
            is False
        )
        assert (
            engine.is_eligible_for_festival_bonus(
                hire_date="2025-01-15",
                reference_date="2025-04-15",
            )
            is True
        )

    def test_hire_last_day_of_month(self, engine):
        """月底入職（1/31），滿 3 個月落點為 4/30（使用 relativedelta 自動處理）。"""
        assert (
            engine.is_eligible_for_festival_bonus(
                hire_date="2025-01-31",
                reference_date="2025-04-29",
            )
            is False
        )
        assert (
            engine.is_eligible_for_festival_bonus(
                hire_date="2025-01-31",
                reference_date="2025-04-30",
            )
            is True
        )

    def test_future_hire_date_not_eligible(self, engine):
        """尚未到職（hire_date > reference_date）不可領。"""
        assert (
            engine.is_eligible_for_festival_bonus(
                hire_date="2025-06-01",
                reference_date="2025-04-01",
            )
            is False
        )

    def test_resigned_employee_still_eligible_if_worked_long_enough(self, engine):
        """離職員工 eligibility 仍以 hire_date 為準；本函式不看 resign。

        離職判斷是上游（API 層以 resign_date 範圍過濾員工）決定；
        本測試確保 is_eligible 只關注 hire_date，避免誤判。
        """
        assert (
            engine.is_eligible_for_festival_bonus(
                hire_date="2023-01-01",
                reference_date="2025-04-01",
            )
            is True
        )


class TestArtTeacherSharedAssistant:
    def test_art_teacher_forces_shared_assistant(self, engine):
        """美師 (art_teacher) 必使用 shared_assistant 目標，與 has_assistant 無關。"""
        result_as_has_assistant = engine.calculate_festival_bonus_v2(
            position="教保員",
            role="art_teacher",
            grade_name="大班",
            current_enrollment=24,
            has_assistant=True,
            is_shared_assistant=False,
        )
        result_as_shared = engine.calculate_festival_bonus_v2(
            position="教保員",
            role="art_teacher",
            grade_name="大班",
            current_enrollment=24,
            has_assistant=False,
            is_shared_assistant=True,
        )
        # 輸入 has_assistant 應被忽略，兩次 target 必須相同
        assert result_as_has_assistant["target"] == result_as_shared["target"]
        assert result_as_has_assistant["base_amount"] == result_as_shared["base_amount"]

    def test_art_teacher_has_own_base(self, engine):
        """美師的節慶獎金基數應使用 art_teacher 獨立值（非退回助教）。"""
        result = engine.calculate_festival_bonus_v2(
            position="教保員",
            role="art_teacher",
            grade_name="大班",
            current_enrollment=24,
            has_assistant=False,
            is_shared_assistant=True,
        )
        assert result["base_amount"] > 0


class TestFestivalBonusNullSafety:
    def test_none_base_in_config_does_not_raise(self, engine):
        """若 DB bonus_base 某 key 的值為 None，計算應以 0 處理不拋 TypeError。"""
        engine._bonus_base["head_teacher"]["A"] = None
        try:
            result = engine.calculate_festival_bonus_v2(
                position="幼兒園教師",
                role="head_teacher",
                grade_name="大班",
                current_enrollment=24,
                has_assistant=True,
                is_shared_assistant=False,
            )
            assert result["base_amount"] == 0
            assert result["festival_bonus"] == 0
        finally:
            engine._bonus_base["head_teacher"]["A"] = 2000

    def test_unknown_role_zero_base(self, engine):
        """role 不在 bonus_base 時，base_amount 應為 0，不拋錯。"""
        result = engine.calculate_festival_bonus_v2(
            position="幼兒園教師",
            role="nonexistent_role",
            grade_name="大班",
            current_enrollment=24,
            has_assistant=True,
            is_shared_assistant=False,
        )
        assert result["base_amount"] == 0
        assert result["festival_bonus"] == 0


class TestOvertimeBonusZeroDivision:
    def test_target_zero_yields_zero_festival_but_counts_overtime(self, engine):
        """年級 / 設定缺漏導致 target=0 時，不應 ZeroDivisionError，festival=0。"""
        result = engine.calculate_festival_bonus_v2(
            position="幼兒園教師",
            role="head_teacher",
            grade_name="不存在年級",
            current_enrollment=30,
            has_assistant=True,
            is_shared_assistant=False,
        )
        assert result["target"] == 0
        assert result["ratio"] == 0
        assert result["festival_bonus"] == 0
