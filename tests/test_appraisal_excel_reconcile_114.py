"""考核引擎「114學年上對帳鎖」——對齊 Excel 的獎金率 + 有效日期正確解析。

與 test_appraisal_engine.py 的差異（不重複）：
  - BonusRateLookup 使用 apxlal01 migration 所寫的 **2025-08-01** effective_from
    （對應 114上 base_score_calc_date ≈ 2025-09-15）
  - 驗證 ASSISTANT OUTSTANDING 的新對齊值 5500（原 4500→5500，是 Task-1 的核心改動）
  - 驗證 effective-date silent-0 修正：舊 2026-08-01-only lookup 對 2025-09-15 回 None
  - 蔡宜倩 / 蔡佩汶-style 回歸：確認對齊後 HEAD_TEACHER 值未被破壞
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from models.appraisal import Grade, RoleGroup
from services.appraisal.engine import BonusRateLookup, compute_summary

# ---------------------------------------------------------------------------
# 對齊 apxlal01 的 ALIGNED_RATES（effective_from='2025-08-01'）
# ---------------------------------------------------------------------------


@pytest.fixture
def aligned_rates_2025() -> BonusRateLookup:
    """對齊 migration apxlal01 所寫的 2025-08-01 獎金率表（全 10 組）。"""
    return BonusRateLookup(
        rates={
            ("2025-08-01", RoleGroup.SUPERVISOR, Grade.OUTSTANDING): Decimal("8000"),
            ("2025-08-01", RoleGroup.SUPERVISOR, Grade.GOOD): Decimal("5000"),
            ("2025-08-01", RoleGroup.HEAD_TEACHER, Grade.OUTSTANDING): Decimal("6000"),
            ("2025-08-01", RoleGroup.HEAD_TEACHER, Grade.GOOD): Decimal("4000"),
            ("2025-08-01", RoleGroup.ASSISTANT, Grade.OUTSTANDING): Decimal("5500"),
            ("2025-08-01", RoleGroup.ASSISTANT, Grade.GOOD): Decimal("3500"),
            ("2025-08-01", RoleGroup.STAFF, Grade.OUTSTANDING): Decimal("6000"),
            ("2025-08-01", RoleGroup.STAFF, Grade.GOOD): Decimal("4000"),
            ("2025-08-01", RoleGroup.COOK, Grade.OUTSTANDING): Decimal("6000"),
            ("2025-08-01", RoleGroup.COOK, Grade.GOOD): Decimal("4000"),
        }
    )


# ---------------------------------------------------------------------------
# Part A-1：蔡宜倩回歸 — HEAD_TEACHER 4000 未被新對齊值破壞
# ---------------------------------------------------------------------------


class TestCaiYiqianRegression:
    def test_cai_yiqian_head_teacher_good_with_2025_rates(self, aligned_rates_2025):
        """蔡宜倩：base 75.6 + deltas[-3.0,-0.25,6.0,2.0,2.0] = 82.35 甲等
        → bonus 4000 × 82.35% = 3294.00（HEAD_TEACHER 4000 在對齊後不變）。
        """
        result = compute_summary(
            actual_enrollment=121,
            enrollment_target=160,
            score_deltas=[
                Decimal("-3.0"),
                Decimal("-0.25"),
                Decimal("6.0"),
                Decimal("2.0"),
                Decimal("2.0"),
            ],
            role_group=RoleGroup.HEAD_TEACHER,
            bonus_rates=aligned_rates_2025,
            on_date=date(2025, 9, 15),  # 114上 — 對齊 base_score_calc_date
        )
        assert result.base_score == Decimal("75.6")
        assert result.total_score == Decimal("82.35")
        assert result.grade == Grade.GOOD
        assert result.bonus_amount == Decimal("3294.00")


# ---------------------------------------------------------------------------
# Part A-2：蔡佩汶-style 丁等回歸 — 不發獎金
# ---------------------------------------------------------------------------


class TestFailGradeNoBonus:
    def test_fail_grade_zero_bonus(self, aligned_rates_2025):
        """丁等（total < 60）→ bonus = 0.00，無論 rates 有無對應 rate。"""
        result = compute_summary(
            actual_enrollment=121,
            enrollment_target=160,
            score_deltas=[
                Decimal("-13.0"),
                Decimal("-1.5"),
                Decimal("-2.0"),
                Decimal("-2.0"),
            ],
            role_group=RoleGroup.HEAD_TEACHER,
            bonus_rates=aligned_rates_2025,
            on_date=date(2025, 9, 15),
        )
        assert result.total_score == Decimal("57.10")
        assert result.grade == Grade.FAIL
        assert result.bonus_amount == Decimal("0.00")


# ---------------------------------------------------------------------------
# Part A-3：ASSISTANT OUTSTANDING — 新對齊值 5500（Task-1 核心改動）
# ---------------------------------------------------------------------------


class TestAssistantOutstandingAligned:
    def test_assistant_outstanding_uses_5500(self, aligned_rates_2025):
        """ASSISTANT 優等：base 100，無加減分 → total 100 → 優等
        → bonus 5500 × 100% = 5500.00（apxlal01 對齊值；原為 4500）。
        """
        result = compute_summary(
            actual_enrollment=160,
            enrollment_target=160,
            score_deltas=[],
            role_group=RoleGroup.ASSISTANT,
            bonus_rates=aligned_rates_2025,
            on_date=date(2025, 9, 15),
        )
        assert result.base_score == Decimal("100.0")
        assert result.grade == Grade.OUTSTANDING
        assert result.bonus_amount == Decimal("5500.00")

    def test_assistant_outstanding_old_rates_would_be_4500(self):
        """舊 seed（2026-08-01-only lookup）不能解析 2025-09-15，回傳 bonus=0.
        這是 apxlal01 所修復的 silent-0 bug。此測試鎖定修復前的失效行為。
        """
        old_rates = BonusRateLookup(
            rates={
                # 僅有 2026-08-01，無 2025-08-01
                ("2026-08-01", RoleGroup.ASSISTANT, Grade.OUTSTANDING): Decimal("4500"),
            }
        )
        # 對 on_date=2025-09-15，effective_from='2026-08-01' > '2025-09-15' → None
        resolved = old_rates.resolve(
            date(2025, 9, 15), RoleGroup.ASSISTANT, Grade.OUTSTANDING
        )
        assert resolved is None  # 修復前：silent 0


# ---------------------------------------------------------------------------
# Part A-4：effective-date — 2025-08-01 rate 可被 2025-09-15 解到
# ---------------------------------------------------------------------------


class TestEffectiveDateResolution:
    def test_effective_2025_08_01_resolves_for_2025_09_15(self, aligned_rates_2025):
        """BonusRateLookup.resolve() 對 on_date=2025-09-15 應回傳 2025-08-01 的 rate。
        確認 effective-date 判斷邏輯：最大 effective_from ≤ on_date 的那筆。
        """
        resolved = aligned_rates_2025.resolve(
            date(2025, 9, 15), RoleGroup.ASSISTANT, Grade.OUTSTANDING
        )
        assert resolved == Decimal("5500")

    def test_effective_2025_08_01_resolves_for_2026_03_15(self, aligned_rates_2025):
        """114下 (2026-03-15) 也能解析到 2025-08-01 的 rate（無更新版時走最近前版）。"""
        resolved = aligned_rates_2025.resolve(
            date(2026, 3, 15), RoleGroup.HEAD_TEACHER, Grade.GOOD
        )
        assert resolved == Decimal("4000")
