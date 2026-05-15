"""半年考核引擎（services/appraisal/engine.py）單元測試。

純函式測試，無 DB 依賴；對應 Excel 14 員工資料 + 5-step 計算結果。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from models.appraisal import Grade, RoleGroup
from services.appraisal.engine import (
    BonusRateLookup,
    classify_grade,
    compute_base_score,
    compute_bonus_amount,
    compute_summary,
    compute_total_score,
    proration_rate,
    sum_score_items,
)


class TestComputeBaseScore:
    def test_excel_example_121_over_160(self):
        # Excel「9/15 分數 = 121/160 = 75.6%」
        assert compute_base_score(121, 160) == Decimal("75.6")

    def test_full_target_100_pct(self):
        assert compute_base_score(160, 160) == Decimal("100.0")

    def test_target_zero_returns_zero_with_warning(self, caplog):
        assert compute_base_score(50, 0) == Decimal("0.0")
        assert "不合法" in caplog.text

    def test_negative_actual_treated_as_zero(self):
        assert compute_base_score(-10, 160) == Decimal("0.0")


class TestSumScoreItems:
    def test_excel_example_wang_yaling(self):
        # Excel 王雅玲：未打卡 -0.75 + 9/15休學 -1.7 + 幼兒意外 -2.0 = -4.45
        deltas = [Decimal("-0.75"), Decimal("-1.7"), Decimal("-2.0")]
        assert sum_score_items(deltas) == Decimal("-4.45")

    def test_mixed_pos_neg(self):
        deltas = [Decimal("6"), Decimal("2"), Decimal("-3"), Decimal("4")]
        assert sum_score_items(deltas) == Decimal("9.00")

    def test_skips_none(self):
        deltas = [Decimal("5"), None, Decimal("-2")]
        assert sum_score_items(deltas) == Decimal("3.00")

    def test_accepts_float_and_int(self):
        deltas = [1.5, 2, Decimal("0.5")]
        assert sum_score_items(deltas) == Decimal("4.00")


class TestComputeTotalScore:
    def test_excel_wang_yaling(self):
        # 75.6 + (-4.45) = 71.15
        assert compute_total_score(Decimal("75.6"), Decimal("-4.45")) == Decimal(
            "71.15"
        )

    def test_clamps_above_max(self):
        assert compute_total_score(Decimal("75.6"), Decimal("100")) == Decimal("110.00")

    def test_clamps_below_min(self):
        assert compute_total_score(Decimal("10"), Decimal("-50")) == Decimal("0.00")


class TestClassifyGrade:
    @pytest.mark.parametrize(
        "score, expected",
        [
            (Decimal("100"), Grade.OUTSTANDING),
            (Decimal("90"), Grade.OUTSTANDING),
            (Decimal("89.99"), Grade.GOOD),
            (Decimal("80"), Grade.GOOD),
            (Decimal("79.99"), Grade.PASS),
            (Decimal("70"), Grade.PASS),
            (Decimal("69.99"), Grade.WARN),
            (Decimal("60"), Grade.WARN),
            (Decimal("59.99"), Grade.FAIL),
            (Decimal("0"), Grade.FAIL),
        ],
    )
    def test_thresholds(self, score, expected):
        assert classify_grade(score) == expected

    def test_excel_wang_yaling_pass_grade(self):
        # 71.15 → 乙等 (PASS)
        assert classify_grade(Decimal("71.15")) == Grade.PASS


class TestComputeBonusAmount:
    @pytest.fixture
    def rates(self):
        # 對應 seed 後的 bonus_rates 表（M1 a3p4p5r6i7s8）
        return BonusRateLookup(
            rates={
                ("2026-08-01", RoleGroup.SUPERVISOR, Grade.OUTSTANDING): Decimal("8000"),
                ("2026-08-01", RoleGroup.SUPERVISOR, Grade.GOOD): Decimal("5000"),
                ("2026-08-01", RoleGroup.HEAD_TEACHER, Grade.OUTSTANDING): Decimal("6000"),
                ("2026-08-01", RoleGroup.HEAD_TEACHER, Grade.GOOD): Decimal("4000"),
                ("2026-08-01", RoleGroup.ASSISTANT, Grade.OUTSTANDING): Decimal("4500"),
                ("2026-08-01", RoleGroup.ASSISTANT, Grade.GOOD): Decimal("3000"),
            }
        )

    def test_excel_cai_yiqian_head_teacher_good(self, rates):
        # 蔡宜倩 82.35 甲等 → 4000 × 82.35 / 100 = 3294.00
        bonus = compute_bonus_amount(
            total_score=Decimal("82.35"),
            grade=Grade.GOOD,
            role_group=RoleGroup.HEAD_TEACHER,
            bonus_rates=rates,
            on_date=date(2026, 9, 15),
        )
        assert bonus == Decimal("3294.00")

    def test_excel_chen_pinfen_head_teacher_good(self, rates):
        # 陳品棻 83.1 甲等 → 4000 × 83.1 / 100 = 3324.00
        bonus = compute_bonus_amount(
            total_score=Decimal("83.10"),
            grade=Grade.GOOD,
            role_group=RoleGroup.HEAD_TEACHER,
            bonus_rates=rates,
            on_date=date(2026, 9, 15),
        )
        assert bonus == Decimal("3324.00")

    def test_no_bonus_for_pass_grade(self, rates):
        # 乙等不發獎金
        bonus = compute_bonus_amount(
            total_score=Decimal("71.15"),
            grade=Grade.PASS,
            role_group=RoleGroup.HEAD_TEACHER,
            bonus_rates=rates,
            on_date=date(2026, 9, 15),
        )
        assert bonus == Decimal("0.00")

    def test_no_bonus_for_fail_grade(self, rates):
        bonus = compute_bonus_amount(
            total_score=Decimal("57.10"),
            grade=Grade.FAIL,
            role_group=RoleGroup.HEAD_TEACHER,
            bonus_rates=rates,
            on_date=date(2026, 9, 15),
        )
        assert bonus == Decimal("0.00")

    def test_supervisor_outstanding(self, rates):
        # 8000 × 92 / 100 = 7360.00
        bonus = compute_bonus_amount(
            total_score=Decimal("92"),
            grade=Grade.OUTSTANDING,
            role_group=RoleGroup.SUPERVISOR,
            bonus_rates=rates,
            on_date=date(2026, 9, 15),
        )
        assert bonus == Decimal("7360.00")

    def test_no_matching_rate_returns_zero(self, rates):
        # STAFF 在 rates 中未提供 → 0
        bonus = compute_bonus_amount(
            total_score=Decimal("85"),
            grade=Grade.GOOD,
            role_group=RoleGroup.STAFF,
            bonus_rates=rates,
            on_date=date(2026, 9, 15),
        )
        assert bonus == Decimal("0.00")


class TestProrationRate:
    def test_full_half_year(self):
        assert proration_rate(Decimal("6")) == Decimal("1.0000")

    def test_three_months(self):
        assert proration_rate(Decimal("3")) == Decimal("0.5000")

    def test_clamp_above_one(self):
        assert proration_rate(Decimal("10")) == Decimal("1")

    def test_clamp_below_zero(self):
        assert proration_rate(Decimal("-1")) == Decimal("0")


class TestComputeSummaryIntegration:
    """整合測試：5 step 跑完一次，對齊 Excel 真實員工結果。"""

    @pytest.fixture
    def rates(self):
        return BonusRateLookup(
            rates={
                ("2026-08-01", RoleGroup.HEAD_TEACHER, Grade.OUTSTANDING): Decimal("6000"),
                ("2026-08-01", RoleGroup.HEAD_TEACHER, Grade.GOOD): Decimal("4000"),
            }
        )

    def test_cai_yiqian_full_pipeline(self, rates):
        # 蔡宜倩 base 75.6，加減分 +6.75 → total 82.35 → 甲等 → 3294
        # Excel: 75.6 - 3.0 - 0.25 + 6.0 + 2.0 + 2.0 = 82.35
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
            bonus_rates=rates,
            on_date=date(2026, 9, 15),
        )
        assert result.base_score == Decimal("75.6")
        assert result.event_score_sum == Decimal("6.75")
        assert result.total_score == Decimal("82.35")
        assert result.grade == Grade.GOOD
        assert result.bonus_amount == Decimal("3294.00")

    def test_cai_peiwen_full_pipeline_fail(self, rates):
        # 蔡佩汶 base 75.6，加減分 -18.5 → total 57.10 → 丁等 → 0
        # Excel: 75.6 - 13.0 - 1.5 - 2.0 - 2.0 = 57.1
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
            bonus_rates=rates,
            on_date=date(2026, 9, 15),
        )
        assert result.total_score == Decimal("57.10")
        assert result.grade == Grade.FAIL
        assert result.bonus_amount == Decimal("0.00")
