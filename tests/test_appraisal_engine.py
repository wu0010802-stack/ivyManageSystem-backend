"""半年考核計算引擎單元測試（純函式，不碰 DB）。

對應 services/appraisal/engine.py 與 services/appraisal/constants.py。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from models.appraisal import Grade, RoleGroup
from services.appraisal.constants import GRADE_BONUS_PCT, MAX_TOTAL_SCORE, MIN_TOTAL_SCORE
from services.appraisal.cycle_dates import (
    default_calc_date,
    default_cycle_dates,
    suggest_role_group,
)
from services.appraisal.engine import (
    classify_grade,
    compute_bonus_amount,
    compute_total_score,
)


class TestClassifyGrade:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (Decimal("100"), Grade.OUTSTANDING),
            (Decimal("90"), Grade.OUTSTANDING),
            (Decimal("89.99"), Grade.GOOD),
            (Decimal("80"), Grade.GOOD),
            (Decimal("70"), Grade.PASS),
            (Decimal("69.99"), Grade.WARN),
            (Decimal("60"), Grade.WARN),
            (Decimal("59"), Grade.FAIL),
            (Decimal("0"), Grade.FAIL),
        ],
    )
    def test_grade_切點(self, score, expected):
        assert classify_grade(score) == expected


class TestComputeTotalScore:
    def test_正常加總(self):
        assert compute_total_score(Decimal("80"), Decimal("5")) == Decimal("85.00")

    def test_clamp_上限(self):
        assert compute_total_score(Decimal("100"), Decimal("20")) == MAX_TOTAL_SCORE

    def test_clamp_下限(self):
        assert compute_total_score(Decimal("10"), Decimal("-30")) == MIN_TOTAL_SCORE


class TestComputeBonusAmount:
    @pytest.fixture
    def rates_map(self):
        return {
            (RoleGroup.SUPERVISOR, Grade.OUTSTANDING): Decimal("10000"),
            (RoleGroup.SUPERVISOR, Grade.GOOD): Decimal("5000"),
            (RoleGroup.HEAD_TEACHER, Grade.OUTSTANDING): Decimal("8000"),
            (RoleGroup.HEAD_TEACHER, Grade.GOOD): Decimal("4000"),
            (RoleGroup.HEAD_TEACHER, Grade.PASS): Decimal("3000"),
        }

    def test_優等_主管_拿_100pct(self, rates_map):
        assert (
            compute_bonus_amount(Grade.OUTSTANDING, RoleGroup.SUPERVISOR, rates_map)
            == Decimal("10000.00")
        )

    def test_甲等_班導_拿_80pct(self, rates_map):
        # base 4000 × 0.80 = 3200
        assert (
            compute_bonus_amount(Grade.GOOD, RoleGroup.HEAD_TEACHER, rates_map)
            == Decimal("3200.00")
        )

    def test_乙等_班導_拿_60pct(self, rates_map):
        # base 3000 × 0.60 = 1800
        assert (
            compute_bonus_amount(Grade.PASS, RoleGroup.HEAD_TEACHER, rates_map)
            == Decimal("1800.00")
        )

    def test_丙等_拿_0(self, rates_map):
        assert (
            compute_bonus_amount(Grade.WARN, RoleGroup.HEAD_TEACHER, rates_map)
            == Decimal("0")
        )

    def test_丁等_拿_0(self, rates_map):
        assert (
            compute_bonus_amount(Grade.FAIL, RoleGroup.HEAD_TEACHER, rates_map)
            == Decimal("0")
        )

    def test_缺_rate_回_0_不抛(self, rates_map):
        # COOK 在 rates_map 沒設定 → 不擋計算
        assert (
            compute_bonus_amount(Grade.OUTSTANDING, RoleGroup.COOK, rates_map)
            == Decimal("0")
        )


class TestGradePct:
    """確認 GRADE_BONUS_PCT 完整覆蓋 5 個等第。"""

    def test_5_grades_all_present(self):
        assert set(GRADE_BONUS_PCT.keys()) == set(Grade)

    def test_優甲乙_遞減(self):
        assert (
            GRADE_BONUS_PCT[Grade.OUTSTANDING]
            > GRADE_BONUS_PCT[Grade.GOOD]
            > GRADE_BONUS_PCT[Grade.PASS]
        )


class TestCycleDates:
    def test_第一學期_民國114(self):
        start, end, calc = default_cycle_dates(114, "FIRST")
        assert (start.year, start.month, start.day) == (2025, 8, 1)
        assert (end.year, end.month, end.day) == (2026, 1, 31)
        assert (calc.year, calc.month, calc.day) == (2025, 9, 15)

    def test_第二學期_民國114(self):
        start, end, calc = default_cycle_dates(114, "SECOND")
        assert (start.year, start.month, start.day) == (2026, 2, 1)
        assert (end.year, end.month, end.day) == (2026, 7, 31)
        assert (calc.year, calc.month, calc.day) == (2026, 3, 15)

    def test_calc_date_helper(self):
        assert default_calc_date("FIRST", 2025).isoformat() == "2025-09-15"
        assert default_calc_date("SECOND", 2026).isoformat() == "2026-03-15"


class TestSuggestRoleGroup:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("園長", RoleGroup.SUPERVISOR),
            ("總園長", RoleGroup.SUPERVISOR),
            ("教學主任", RoleGroup.SUPERVISOR),
            ("廚工", RoleGroup.COOK),
            ("廚房助理", RoleGroup.COOK),
            ("行政會計", RoleGroup.STAFF),
            ("秘書", RoleGroup.STAFF),
            ("班導師", RoleGroup.HEAD_TEACHER),
            ("副班導師", RoleGroup.ASSISTANT),
            ("教保員", RoleGroup.ASSISTANT),
            (None, RoleGroup.ASSISTANT),
            ("", RoleGroup.ASSISTANT),
        ],
    )
    def test_推薦(self, title, expected):
        assert suggest_role_group(title) == expected
