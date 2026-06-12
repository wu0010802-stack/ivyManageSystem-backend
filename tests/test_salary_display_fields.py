import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.salary_fields import calculate_display_bonus_total, build_history_breakdown


class TestCalculateDisplayBonusTotal:
    def test_sums_bonus_fields_without_double_counting_bonus_amount(self):
        """festival/overtime 不可再被 bonus_amount 重複加總。"""
        record = SimpleNamespace(
            festival_bonus=1200,
            overtime_bonus=800,
            performance_bonus=500,
            special_bonus=300,
            bonus_amount=3500,
            supervisor_dividend=1500,
        )

        result = calculate_display_bonus_total(record)

        assert result == 2800

    def test_treats_missing_values_as_zero(self):
        """None 欄位應視為 0，避免 API 顯示 NaN 或 TypeError。"""
        record = SimpleNamespace(
            festival_bonus=None,
            overtime_bonus=None,
            performance_bonus=None,
            special_bonus=None,
        )

        result = calculate_display_bonus_total(record)

        assert result == 0


class TestBuildHistoryBreakdown:
    def _record(self, **over):
        base = dict(
            base_salary=2950,
            hourly_total=0,
            performance_bonus=0,
            special_bonus=0,
            supervisor_dividend=5000,
            overtime_pay=0,
            meeting_overtime_pay=0,
            birthday_bonus=0,
            extra_allowance=0,
            extra_allowance_label=None,
            festival_bonus=26000,
            overtime_bonus=0,
            appraisal_year_end_bonus=0,
            unused_leave_payout=0,
            labor_insurance_employee=600,
            health_insurance_employee=800,
            supplementary_health_employee=0,
            pension_employee=0,
            late_deduction=0,
            early_leave_deduction=0,
            missing_punch_deduction=0,
            leave_deduction=900,
            absence_deduction=0,
            other_deduction=0,
            gross_salary=7950,
            # total_deduction 刻意與個別扣款欄位合計(2300)不符，
            # 驗證函式使用 persisted total_deduction 而非重算。
            total_deduction=4604,
            net_salary=3346,
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_income_subtotal_is_persisted_gross(self):
        bd = build_history_breakdown(self._record())
        assert bd["income_subtotal"] == 7950

    def test_deduction_subtotal_is_persisted_total(self):
        bd = build_history_breakdown(self._record())
        assert bd["deduction_subtotal"] == 4604

    def test_net_equals_gross_minus_deduction(self):
        bd = build_history_breakdown(self._record())
        assert bd["net_salary"] == bd["income_subtotal"] - bd["deduction_subtotal"]

    def test_net_is_persisted_not_recomputed(self):
        """net_salary 取 persisted 值，不由 gross − total_deduction 重算。"""
        rec = self._record(net_salary=99999)  # gross−deduction=3346，但 persisted=99999
        bd = build_history_breakdown(rec)
        assert bd["net_salary"] == 99999

    def test_supervisor_dividend_in_income_not_separate(self):
        """釘住易錯點：主管紅利進實發（income），不在另行轉帳。"""
        bd = build_history_breakdown(self._record())
        income_keys = {l["key"] for l in bd["income"]}
        sep_keys = {l["key"] for l in bd["separate_transfer"]}
        assert "supervisor_dividend" in income_keys
        assert "supervisor_dividend" not in sep_keys

    def test_festival_overtime_in_separate_not_income(self):
        """釘住易錯點：節慶/超額為另行轉帳，不進 income。"""
        bd = build_history_breakdown(self._record())
        income_keys = {l["key"] for l in bd["income"]}
        sep_keys = {l["key"] for l in bd["separate_transfer"]}
        assert {"festival_bonus", "overtime_bonus"} <= sep_keys
        assert "festival_bonus" not in income_keys

    def test_income_lines_sum_to_subtotal_via_other(self):
        """seed 殘差：gross 比已知收入多 5000 → other_income 吸收，使收入區對得回應發。"""
        rec = self._record(
            supervisor_dividend=0,
            gross_salary=35000,
            base_salary=29500,
            birthday_bonus=500,
        )
        bd = build_history_breakdown(rec)
        assert sum(l["amount"] for l in bd["income"]) == bd["income_subtotal"]
        other = next(l for l in bd["income"] if l["key"] == "other_income")
        assert other["amount"] == 5000

    def test_supplementary_health_is_informational_child_not_double_counted(self):
        """補充保費為健保下 informational 子列，不另計、不改扣款合計。"""
        rec = self._record(supplementary_health_employee=200)
        bd = build_history_breakdown(rec)
        health = next(
            l for l in bd["deductions"] if l["key"] == "health_insurance_employee"
        )
        assert health["children"][0]["informational"] is True
        assert health["children"][0]["amount"] == 200
        assert bd["deduction_subtotal"] == 4604
        assert all(
            l["key"] != "supplementary_health_employee" for l in bd["deductions"]
        )

    def test_extra_allowance_label_as_note(self):
        rec = self._record(extra_allowance=1500, extra_allowance_label="值週")
        bd = build_history_breakdown(rec)
        extra = next(l for l in bd["income"] if l["key"] == "extra_allowance")
        assert extra["note"] == "值週"

    def test_none_values_coalesced_to_zero(self):
        rec = self._record(performance_bonus=None, late_deduction=None)
        bd = build_history_breakdown(rec)  # 不可拋例外
        assert isinstance(bd["income_subtotal"], float)
