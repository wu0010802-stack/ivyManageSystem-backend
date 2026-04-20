"""
多職位切換 / 職位屬性覆蓋 行為鎖定測試。

Employee schema 僅保留「當下」職位資訊（title / position / supervisor_role /
bonus_grade），無歷史分段紀錄。因此月中職位切換的實際行為是：
計算當月薪資時，以「計算時的」員工職位屬性為準，不做按日分段。

這些測試鎖定目前行為，避免未來無意改壞。
"""

import pytest

from services.salary_engine import SalaryEngine


class TestBonusGradeOverride:
    """bonus_grade 覆蓋節慶獎金職稱等級"""

    def test_override_a_forces_teacher_grade(self, engine):
        # 助理教保員（C 級）被 bonus_grade='A' 覆蓋為教師（A 級）
        effective = engine._get_effective_bonus_title("助理教保員", "A")
        assert effective == "幼兒園教師"

    def test_override_b_forces_childcare(self, engine):
        assert engine._get_effective_bonus_title("幼兒園教師", "B") == "教保員"

    def test_override_c_forces_assistant(self, engine):
        assert engine._get_effective_bonus_title("幼兒園教師", "C") == "助理教保員"

    def test_lowercase_override_accepted(self, engine):
        assert engine._get_effective_bonus_title("助理教保員", "a") == "幼兒園教師"

    def test_none_override_keeps_original(self, engine):
        assert engine._get_effective_bonus_title("幼兒園教師", None) == "幼兒園教師"

    def test_empty_override_keeps_original(self, engine):
        assert engine._get_effective_bonus_title("幼兒園教師", "") == "幼兒園教師"

    def test_unknown_grade_keeps_original(self, engine):
        # 未知 grade 時不做覆蓋，回原 title
        assert engine._get_effective_bonus_title("幼兒園教師", "Z") == "幼兒園教師"


class TestSupervisorRoleSwitch:
    """supervisor_role 切換時 supervisor_dividend 即時變動"""

    def test_no_supervisor_role(self, engine):
        assert engine.get_supervisor_dividend("幼兒園教師") == 0

    def test_assigned_principal_role(self, engine):
        # title 仍為幼兒園教師，但 supervisor_role 設為園長 → 享園長紅利
        assert (
            engine.get_supervisor_dividend(
                "幼兒園教師", position="", supervisor_role="園長"
            )
            == 5000
        )

    def test_switch_principal_to_director(self, engine):
        as_principal = engine.get_supervisor_dividend(
            "幼兒園教師", supervisor_role="園長"
        )
        as_director = engine.get_supervisor_dividend(
            "幼兒園教師", supervisor_role="主任"
        )
        assert as_principal == 5000
        assert as_director == 4000
        assert as_principal != as_director

    def test_remove_supervisor_role_drops_to_zero(self, engine):
        with_role = engine.get_supervisor_dividend("幼兒園教師", supervisor_role="主任")
        without_role = engine.get_supervisor_dividend("幼兒園教師")
        assert with_role == 4000
        assert without_role == 0


class TestPositionVsTitlePriority:
    """position 與 supervisor_role 對 title 的優先順序"""

    def test_position_wins_over_title_for_dividend(self, engine):
        # title 是教師，position 指定園長 → 取 position 的園長紅利
        assert engine.get_supervisor_dividend("幼兒園教師", position="園長") == 5000

    def test_supervisor_role_and_position_both_set(self, engine):
        # supervisor_role 與 position 同時指向主管職時，仍能正確回傳
        result = engine.get_supervisor_dividend(
            "幼兒園教師", position="主任", supervisor_role="主任"
        )
        assert result == 4000


class TestProrationWithPosition:
    """職位切換時，底薪折算仍以「實際在職天數」為準（與職位無關）"""

    def test_hire_and_resign_same_month_both_mid(self, engine):
        # 6/5 入職、6/20 離職 → 在職 16 天 / 30 天
        result = engine._prorate_for_period(30000, "2026-06-05", "2026-06-20", 2026, 6)
        assert result == pytest.approx(30000 * 16 / 30)

    def test_mid_month_resign(self, engine):
        # 6/15 離職 → 在職 1–15 共 15 天
        result = engine._prorate_for_period(30000, None, "2026-06-15", 2026, 6)
        assert result == pytest.approx(30000 * 15 / 30)

    def test_resign_on_first_day(self, engine):
        # 1 日離職 → 在職 1 天
        result = engine._prorate_for_period(30000, None, "2026-06-01", 2026, 6)
        assert result == pytest.approx(30000 * 1 / 30)


class TestCalculateSalaryPickUpPositionAtCalcTime:
    """整合層面：calculate_salary 以「當下」員工職位屬性計算，不做分段"""

    def _base_employee(self, **overrides):
        emp = {
            "employee_id": "E001",
            "name": "測試員",
            "title": "幼兒園教師",
            "position": "",
            "employee_type": "regular",
            "base_salary": 30000,
            "hourly_rate": 0,
            "insurance_salary": 30000,
            "dependents": 0,
            "hire_date": "2024-01-01",
        }
        emp.update(overrides)
        return emp

    def test_supervisor_dividend_reflects_current_role(self, engine):
        # 同一員工兩個情境：有 supervisor_role="主任" vs 無
        emp_supervisor = self._base_employee(supervisor_role="主任")
        emp_no_role = self._base_employee()

        bd_supervisor = engine.calculate_salary(emp_supervisor, year=2026, month=4)
        bd_no_role = engine.calculate_salary(emp_no_role, year=2026, month=4)

        assert bd_supervisor.supervisor_dividend == 4000
        assert bd_no_role.supervisor_dividend == 0

    def test_bonus_grade_override_not_reflected_without_context(self, engine):
        """未提供班級 / 辦公室 context 時，calculate_salary 不計節慶獎金。

        鎖定「節慶獎金需依賴外部 context」這個不變量，防止未來誤引入
        bonus_grade 的隱式計算而忽略 context。
        """
        emp = self._base_employee(title="助理教保員", bonus_grade="A")
        bd = engine.calculate_salary(emp, year=2026, month=4)
        assert bd.festival_bonus == 0
