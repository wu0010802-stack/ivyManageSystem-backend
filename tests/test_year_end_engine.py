"""年終獎金引擎（services/year_end/engine.py）單元測試。

對齊 Excel「114年年終經營績效」「年終獎金」「年終獎金總表」三 sheets 的串聯計算。
驗證 case 取自蔡宜倩（HEAD_TEACHER，到職滿一年，含扣項與多種特別獎金）與
郭玟秀（HEAD_TEACHER，育嬰假後到職比例 10/12）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from services.year_end.engine import (
    DeductionBreakdown,
    PerformanceRates,
    compute_avg_performance_rate,
    compute_deduction_total,
    compute_gross_amount,
    compute_payable_amount,
    compute_proration_rate,
    compute_settlement,
    compute_subtotal_amount,
    compute_total_amount,
)


class TestStep1AvgPerformanceRate:
    def test_excel_cai_yiqian_97_0(self):
        # 蔡宜倩 全校(75.6+91.5)/2=83.55、班舊生(92.9+100)/2=96.45、
        # 班經營(106.4+115.3)/2=110.85 → (83.55+96.45+110.85)/3=96.95→97.0
        rates = PerformanceRates(
            school_rate_first=Decimal("75.6"),
            school_rate_second=Decimal("91.5"),
            class_returning_rate_first=Decimal("92.9"),
            class_returning_rate_second=Decimal("100"),
            class_performance_rate_first=Decimal("106.4"),
            class_performance_rate_second=Decimal("115.3"),
        )
        assert compute_avg_performance_rate(rates) == Decimal("97.0")

    def test_excel_guo_wenxiu_86_7(self):
        # 郭玟秀 全校 83.55、班舊生 (75.0+94.9)/2=84.95、班經營 91.45 → avg = 86.65 ≈ 86.7
        rates = PerformanceRates(
            school_rate_first=Decimal("75.6"),
            school_rate_second=Decimal("91.5"),
            class_returning_rate_first=Decimal("75.0"),
            class_returning_rate_second=Decimal("94.9"),
            class_performance_rate_first=Decimal("94.4"),
            class_performance_rate_second=Decimal("88.5"),
        )
        assert compute_avg_performance_rate(rates) == Decimal("86.7")

    def test_office_staff_no_class_performance(self):
        # STAFF/COOK 無班級績效 → 只算全校
        rates = PerformanceRates(
            school_rate_first=Decimal("75.6"),
            school_rate_second=Decimal("91.5"),
        )
        # avg = (75.6+91.5)/2 = 83.55 → 83.6 (1 位小數)
        assert compute_avg_performance_rate(rates) == Decimal("83.6")

    def test_new_hire_only_second_semester(self):
        # 新員工只算下學期 → school = 91.5、班舊生 94.9、班經營 88.5
        # → (91.5+94.9+88.5)/3 = 91.6333 → 91.6
        rates = PerformanceRates(
            school_rate_second=Decimal("91.5"),
            class_returning_rate_second=Decimal("94.9"),
            class_performance_rate_second=Decimal("88.5"),
        )
        assert compute_avg_performance_rate(rates) == Decimal("91.6")

    def test_no_rates_returns_zero(self):
        assert compute_avg_performance_rate(PerformanceRates()) == Decimal("0.0")


class TestStep2GrossAmount:
    def test_excel_cai_yiqian(self):
        # (36160+2000) × 97.0% = 38160 × 0.97 = 37015.20
        assert compute_gross_amount(
            base_salary=Decimal("36160"),
            festival_total=Decimal("2000"),
            avg_performance_rate=Decimal("97.0"),
        ) == Decimal("37015.20")

    def test_excel_lvyu_lijhen_44300(self):
        # 呂麗珍 (44300+6500) × 89.6 / 100 = 50800 × 0.896 = 45516.80
        assert compute_gross_amount(
            base_salary=Decimal("44300"),
            festival_total=Decimal("6500"),
            avg_performance_rate=Decimal("89.6"),
        ) == Decimal("45516.80")


class TestStep3SubtotalAmount:
    def test_excel_cai_yiqian_30944_71(self):
        # 37015.2 × 83.6% = 30944.7072 → 30944.71
        assert compute_subtotal_amount(
            gross_amount=Decimal("37015.20"),
            org_achievement_rate=Decimal("83.6"),
        ) == Decimal("30944.71")

    def test_91_5_rate_for_new_hires(self):
        # 王品嬑 達成比率 91.5%
        # 毛額 (29400+0) × 94.4 / 100 = 27753.60
        # 小計 27753.60 × 91.5 / 100 = 25394.5440 → 25394.54
        assert compute_subtotal_amount(
            gross_amount=Decimal("27753.60"),
            org_achievement_rate=Decimal("91.5"),
        ) == Decimal("25394.54")


class TestStep4DeductionTotal:
    def test_excel_cai_yiqian_minus_1900(self):
        # 蔡宜倩 奬懲 -1000、遲到 -900 → -1900
        d = DeductionBreakdown(
            disciplinary=Decimal("-1000"),
            late_early=Decimal("-900"),
        )
        assert compute_deduction_total(d) == Decimal("-1900.00")

    def test_guo_wenxiu_育嬰假大額扣款(self):
        # 郭玟秀 事假 -1000、病假/育嬰 -7500、遲到 -1600 → -10100
        d = DeductionBreakdown(
            personal_leave=Decimal("-1000"),
            sick_leave=Decimal("-7500"),
            late_early=Decimal("-1600"),
        )
        assert compute_deduction_total(d) == Decimal("-10100.00")

    def test_all_zeros(self):
        assert compute_deduction_total(DeductionBreakdown()) == Decimal("0.00")


class TestStep5PayableAmount:
    def test_excel_cai_yiqian_full_year(self):
        # subtotal 30944.71 + deduction -1900 = 29044.71，到職比例 1.0 → 29044.71
        assert compute_payable_amount(
            subtotal_amount=Decimal("30944.71"),
            deduction_total=Decimal("-1900"),
            proration_rate=Decimal("1.0000"),
        ) == Decimal("29044.71")

    def test_excel_guo_wenxiu_10_months(self):
        # 郭玟秀 subtotal 27658.83 + deduction -10100 = 17558.83
        # 到職 10/12 = 0.8333 → 17558.83 × 10/12 = 14632.36 (約)
        # Excel 顯示 14632.354933333329
        result = compute_payable_amount(
            subtotal_amount=Decimal("27658.83"),
            deduction_total=Decimal("-10100"),
            proration_rate=Decimal("0.8333"),
        )
        # 容差 ±1 元
        assert abs(result - Decimal("14632.36")) <= Decimal("1.00")

    def test_proration_clamp(self):
        assert compute_proration_rate(Decimal("12")) == Decimal("1.0000")
        assert compute_proration_rate(Decimal("6")) == Decimal("0.5000")
        assert compute_proration_rate(Decimal("0")) == Decimal("0.0000")
        assert compute_proration_rate(Decimal("-3")) == Decimal("0.0000")
        assert compute_proration_rate(Decimal("15")) == Decimal("1.0000")


class TestStep6TotalAmount:
    def test_excel_cai_yiqian_total_40106(self):
        # payable 29044.71 + 特別獎金 11062.00 = 40106.71
        # 特別獎金合計：113上考核 3312 + 113上紅利 1500 + 113下紅利 1000 +
        # 114上鼓勵才藝 1275 + 114上超額 2000 + 節慶差額 1975 = 11062
        assert compute_total_amount(
            payable_amount=Decimal("29044.71"),
            special_bonus_total=Decimal("11062.00"),
        ) == Decimal("40106.71")

    def test_no_special_bonus(self):
        # 沒特別獎金
        assert compute_total_amount(
            payable_amount=Decimal("25000.00"),
            special_bonus_total=Decimal("0"),
        ) == Decimal("25000.00")


class TestSettlementIntegration:
    """整合測試：6 step 跑完一次，對齊 Excel 真實員工結果（容差 ±1 元）。"""

    def test_cai_yiqian_full_pipeline_40106(self):
        # Excel「年終獎金總表」蔡宜倩：合計 40106.71
        result = compute_settlement(
            base_salary=Decimal("36160"),
            festival_total=Decimal("2000"),
            performance_rates=PerformanceRates(
                school_rate_first=Decimal("75.6"),
                school_rate_second=Decimal("91.5"),
                class_returning_rate_first=Decimal("92.9"),
                class_returning_rate_second=Decimal("100"),
                class_performance_rate_first=Decimal("106.4"),
                class_performance_rate_second=Decimal("115.3"),
            ),
            org_achievement_rate=Decimal("83.6"),
            deductions=DeductionBreakdown(
                disciplinary=Decimal("-1000"),
                late_early=Decimal("-900"),
            ),
            hire_months=Decimal("12"),
            special_bonus_total=Decimal("11062.00"),
        )
        assert result.avg_performance_rate == Decimal("97.0")
        assert result.gross_amount == Decimal("37015.20")
        assert result.subtotal_amount == Decimal("30944.71")
        assert result.deduction_total == Decimal("-1900.00")
        assert result.payable_amount == Decimal("29044.71")
        assert result.total_amount == Decimal("40106.71")

    def test_lvyu_lijhen_supervisor_full_year(self):
        # 呂麗珍 SUPERVISOR，平均績效 89.6、達成 83.6
        # 38052 一直延伸下來最終 = 38052.0448
        result = compute_settlement(
            base_salary=Decimal("44300"),
            festival_total=Decimal("6500"),
            performance_rates=PerformanceRates(
                school_rate_first=Decimal("75.6"),
                school_rate_second=Decimal("91.5"),
                class_returning_rate_first=Decimal("92.6"),
                class_returning_rate_second=Decimal("94.9"),
                class_performance_rate_first=Decimal("94.4"),
                class_performance_rate_second=Decimal("88.5"),
            ),
            org_achievement_rate=Decimal("83.6"),
            deductions=DeductionBreakdown(),
            hire_months=Decimal("12"),
            special_bonus_total=Decimal("0"),
        )
        # Excel 顯示「呂麗珍 38052.0448」— 容差 ±1
        assert abs(result.total_amount - Decimal("38052.04")) <= Decimal("1.00")
