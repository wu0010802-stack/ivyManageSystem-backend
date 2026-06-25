"""P3-I 回歸測試：補充保費 threshold 的 health_insured_salary 須經健保級距正規化。

Bug：_resolve_health_insured_salary 在 emp_dict["health_insured_salary"] 非 None 時
直接採原值（只 clamp 至 health_max_insured），未經 insurance_service.get_bracket
向上取整；而一般健保保費基底是 get_bracket(raw)["amount"]（級距金額）。兩條路徑
對「當月健保投保金額」定義不一致 → threshold(=4×投保額) 偏離，補充保費多扣/少扣
（健保法 §31「當月投保金額」應為級距投保金額）。

既有測試的 fake get_bracket 多為 identity（{"amount": raw}），故照不到此 bug；
本測試用「會向上取整到級距」的 fake 揭露差異。
"""

from services.salary.supplementary_premium import _resolve_health_insured_salary


class _BracketRoundingInsuranceService:
    """get_bracket 向上取整到最近級距（非 identity），模擬真實健保級距行為。"""

    supplementary_health_rate = 0.0211
    health_max_insured = 219500

    # 簡化級距表（取整到下一級）
    _BRACKETS = [30300, 31800, 33300, 34800, 36300, 38200, 40100, 42000, 43900]

    def get_bracket(self, raw):
        for b in self._BRACKETS:
            if raw <= b:
                return {"amount": b}
        return {"amount": self._BRACKETS[-1]}


def test_health_insured_salary_is_bracket_normalized():
    """health_insured_salary=31000（非級距邊界）→ 應正規化為級距 31800，與一般健保口徑一致。"""
    svc = _BracketRoundingInsuranceService()
    emp_dict = {
        "employee_type": "regular",
        "base_salary": 31000,
        "insurance_salary": 31000,
        "health_insured_salary": 31000,
        "health_exempt": False,
    }
    resolved = _resolve_health_insured_salary(emp_dict, svc)
    # 一般健保保費基底會用 get_bracket(31000)["amount"] = 31800；補充保費 threshold
    # 須採同一級距金額，否則 threshold=4×31000 偏低 → excess 偏大 → 多扣補充保費。
    assert resolved == 31800.0, (
        f"health_insured_salary 未經級距正規化：得 {resolved}，應為 31800"
        "（與一般健保 get_bracket 口徑一致）"
    )


def test_bracket_normalized_value_unchanged_when_already_on_boundary():
    """health_insured_salary 已落在級距邊界（43900）→ 正規化後不變。"""
    svc = _BracketRoundingInsuranceService()
    emp_dict = {
        "employee_type": "regular",
        "base_salary": 43900,
        "insurance_salary": 43900,
        "health_insured_salary": 43900,
        "health_exempt": False,
    }
    assert _resolve_health_insured_salary(emp_dict, svc) == 43900.0
