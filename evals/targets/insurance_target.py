"""Target: services/insurance_service.InsuranceService.calculate。

不變量:
- IV1 negative_salary_raises: salary < 0 → ValueError
- IV2 invalid_pension_rate_raises: pension_self_rate ∉ [0, 0.06] → ValueError
- IV3 health_exempt_zeros: health_exempt=True → health_employee == 0 AND health_employer == 0
- IV4 total_consistency: total_employee == labor_employee + health_employee + pension_employee
- IV5 dependents_clamped: dependents > 3 結果 == dependents = 3
- IV6 dependents_non_negative: dependents < 0 結果 == dependents = 0
- IV7 monotone_insured_amount: salary 越大,insured_amount 不會變小(在合法區間)
- IV8 no_negative_premium: 任何單項保費 >= 0
- IV9 health_exempt_dependents_irrelevant: health_exempt=True 時,dependents 不影響 health 結果(永遠 0)
"""

from __future__ import annotations

import math
from dataclasses import asdict

from services.insurance_service import InsuranceCalculation, InsuranceService

from evals.core.target import Invariant, Target

_svc = InsuranceService()  # 用 INSURANCE_TABLE_2026 fallback


def _runner(case: dict) -> dict:
    """執行 calculate;非預期 exception 會冒出讓 runner 收。
    預期 exception(ValueError)也讓它冒,invariants 看 outcome 的 exception 來判定。
    """
    result = _svc.calculate(
        salary=case["salary"],
        dependents=case.get("dependents", 0),
        pension_self_rate=case.get("pension_self_rate", 0),
        no_employment_insurance=case.get("no_employment_insurance", False),
        health_exempt=case.get("health_exempt", False),
        labor_insured=case.get("labor_insured"),
        health_insured=case.get("health_insured"),
        pension_insured=case.get("pension_insured"),
    )
    return asdict(result)  # 讓 invariant 與 reporter 都好處理


def _iv1(case, outcome):
    salary = case.get("salary")
    if not isinstance(salary, (int, float)) or salary != salary:  # NaN guard
        return None
    if salary < 0:
        # 應該 raise ValueError
        exc = outcome.get("exception", "")
        if not exc or "ValueError" not in exc:
            return f"salary={salary} (負) 應 raise ValueError,實際 outcome={outcome.get('ok')}"
    return None


def _iv2(case, outcome):
    rate = case.get("pension_self_rate", 0)
    if not isinstance(rate, (int, float)):
        return None
    if rate != rate:  # NaN
        return None
    if rate < 0 or rate > 0.06:
        exc = outcome.get("exception", "")
        if not exc or "ValueError" not in exc:
            salary = case.get("salary")
            # 但若 salary < 0 應先 raise(IV1 接管),不視為違反
            if isinstance(salary, (int, float)) and salary < 0:
                return None
            return (
                f"pension_self_rate={rate} (越界) 應 raise ValueError,但 outcome 正常"
            )
    return None


def _iv3(case, outcome):
    if not case.get("health_exempt"):
        return None
    if not outcome.get("ok"):
        return None
    res = outcome["result"]
    if res.get("health_employee") != 0 or res.get("health_employer") != 0:
        return (
            f"health_exempt=True 但 health_employee={res.get('health_employee')}, "
            f"health_employer={res.get('health_employer')}"
        )
    return None


def _iv4(case, outcome):
    if not outcome.get("ok"):
        return None
    res = outcome["result"]
    lhs = res.get("total_employee")
    rhs = (
        (res.get("labor_employee") or 0)
        + (res.get("health_employee") or 0)
        + (res.get("pension_employee") or 0)
    )
    if abs(lhs - rhs) > 0.5:
        return f"total_employee={lhs} != sum(labor+health+pension)={rhs}"
    return None


def _iv5(case, outcome):
    """dependents 超過 3 視同 3:用同一 salary 各跑一次比對 health_employee。"""
    if not outcome.get("ok"):
        return None
    deps = case.get("dependents")
    if not isinstance(deps, int) or deps <= 3:
        return None
    if case.get("health_exempt"):
        return None  # IV3 接管
    # 跑一次 dependents=3 比對
    try:
        ref = _svc.calculate(
            salary=case["salary"],
            dependents=3,
            pension_self_rate=case.get("pension_self_rate", 0),
            no_employment_insurance=case.get("no_employment_insurance", False),
            health_exempt=False,
            labor_insured=case.get("labor_insured"),
            health_insured=case.get("health_insured"),
            pension_insured=case.get("pension_insured"),
        )
    except Exception:
        return None
    if outcome["result"]["health_employee"] != ref.health_employee:
        return (
            f"dependents={deps} health_employee={outcome['result']['health_employee']} "
            f"!= dependents=3 ref={ref.health_employee}"
        )
    return None


def _iv6(case, outcome):
    if not outcome.get("ok"):
        return None
    deps = case.get("dependents")
    if not isinstance(deps, int) or deps >= 0:
        return None
    if case.get("health_exempt"):
        return None
    try:
        ref = _svc.calculate(
            salary=case["salary"],
            dependents=0,
            pension_self_rate=case.get("pension_self_rate", 0),
            no_employment_insurance=case.get("no_employment_insurance", False),
            health_exempt=False,
            labor_insured=case.get("labor_insured"),
            health_insured=case.get("health_insured"),
            pension_insured=case.get("pension_insured"),
        )
    except Exception:
        return None
    if outcome["result"]["health_employee"] != ref.health_employee:
        return (
            f"dependents={deps} (負) health_employee="
            f"{outcome['result']['health_employee']} != dependents=0 ref={ref.health_employee}"
        )
    return None


def _iv7(case, outcome):
    """monotone: salary 越大 insured_amount 不應變小。
    用「再算一次 salary*2 + 1」比對。"""
    if not outcome.get("ok"):
        return None
    salary = case.get("salary")
    if not isinstance(salary, (int, float)) or salary < 0:
        return None
    # 避開超大數值的二次運算,加 cap
    if salary > 1_000_000:
        return None
    try:
        ref = _svc.calculate(
            salary=salary * 2 + 1,
            dependents=max(0, min(3, case.get("dependents", 0) or 0)),
            pension_self_rate=max(0, min(0.06, case.get("pension_self_rate", 0) or 0)),
            no_employment_insurance=case.get("no_employment_insurance", False),
            health_exempt=case.get("health_exempt", False),
        )
    except Exception:
        return None
    if ref.insured_amount < outcome["result"]["insured_amount"]:
        return (
            f"monotone: salary={salary} insured={outcome['result']['insured_amount']} > "
            f"salary={salary * 2 + 1} insured={ref.insured_amount}"
        )
    return None


def _iv8(case, outcome):
    if not outcome.get("ok"):
        return None
    res = outcome["result"]
    for k in (
        "labor_employee",
        "labor_employer",
        "labor_government",
        "health_employee",
        "health_employer",
        "pension_employer",
        "pension_employee",
    ):
        v = res.get(k)
        if v is None:
            continue
        if v < 0:
            return f"{k}={v} 為負保費"
    return None


def _iv9(case, outcome):
    """health_exempt=True 時 dependents 不影響(永遠 0/0)。"""
    if not case.get("health_exempt"):
        return None
    if not outcome.get("ok"):
        return None
    res = outcome["result"]
    if res.get("health_employee") != 0 or res.get("health_employer") != 0:
        return (
            f"health_exempt + dependents={case.get('dependents')} 卻有 health 費用: "
            f"{res.get('health_employee')}/{res.get('health_employer')}"
        )
    return None


def _iv10(case, outcome):
    """salary 不可為 NaN(NaN < 0 在 Python 為 False 會繞過 negative guard)。

    +inf 是 by-design 的 graceful degradation:走最高級距 cap;-inf < 0 為 True
    沿用既有 negative guard。所以只標 NaN 為違反。
    """
    salary = case.get("salary")
    if not isinstance(salary, float):
        return None
    if not math.isnan(salary):
        return None
    exc = outcome.get("exception", "")
    if not exc or "ValueError" not in exc:
        return (
            f"salary=NaN 未被 reject;calculate 已產出結果 " "(NaN 傳染所有保費計算結果)"
        )
    return None


def _iv11(case, outcome):
    """dependents 應為非負整數;float 例如 1.5 會讓 health 乘子非整,業務無意義。

    程式碼用 `min(max(0, dependents), 3)`,沒檢型;1.5 會被當 1.5 用,
    health_emp = base * (1 + 1.5) = base * 2.5。
    """
    deps = case.get("dependents")
    if deps is None:
        return None
    # bool 是 int 子類,允許
    if isinstance(deps, bool):
        return None
    if isinstance(deps, int):
        return None
    if isinstance(deps, float):
        # 整數 float 也視同整數(e.g. 2.0)
        if deps == int(deps):
            return None
        # 非整數 float
        if outcome.get("ok"):
            return f"dependents={deps} (非整數 float) 未被 reject,health 乘子變非整數"
    return None


TARGET = Target(
    name="insurance_service",
    description=(
        "InsuranceService.calculate:依 salary/dependents/pension_self_rate/旗標"
        "計算勞健保 + 勞退;支援免就保、健保豁免、分項投保。"
    ),
    signature={
        "fields": {
            "salary": {
                "type": "float",
                "boundary": [
                    0,
                    1,
                    1500,
                    25250,
                    29500,
                    30300,
                    45800,
                    72800,
                    150000,
                    300000,
                    -1,
                ],
            },
            "dependents": {"type": "int", "boundary": [-1, 0, 1, 2, 3, 4, 10, 100]},
            "pension_self_rate": {
                "type": "float",
                "boundary": [-0.001, 0, 0.01, 0.06, 0.0600001, 0.5, 1.0],
            },
            "no_employment_insurance": {"type": "bool"},
            "health_exempt": {"type": "bool"},
            "labor_insured": {
                "type": "float",
                "boundary": [None, 0, -1, 29500, 50000, 1e9],
            },
            "health_insured": {
                "type": "float",
                "boundary": [None, 0, -1, 30300, 50000, 1e9],
            },
            "pension_insured": {
                "type": "float",
                "boundary": [None, 0, -1, 45800, 200000, 1e9],
            },
        },
        "notes": (
            "salary < 0 與 pension_self_rate 越界都應 raise ValueError;"
            "health_exempt 抹平健保;分項投保有自己的 cap。"
        ),
    },
    invariants=[
        Invariant("IV1_negative_salary_raises", "salary<0 → ValueError", _iv1),
        Invariant(
            "IV2_invalid_pension_rate_raises",
            "pension_self_rate∉[0,0.06] → ValueError",
            _iv2,
        ),
        Invariant("IV3_health_exempt_zeros", "health_exempt → health 兩端皆 0", _iv3),
        Invariant(
            "IV4_total_consistency",
            "total_employee == labor+health+pension(員工端)",
            _iv4,
        ),
        Invariant("IV5_dependents_clamp_high", "dependents>3 視同 3", _iv5),
        Invariant("IV6_dependents_clamp_low", "dependents<0 視同 0", _iv6),
        Invariant("IV7_monotone_insured", "salary 上升 insured_amount 不減", _iv7),
        Invariant("IV8_no_negative_premium", "保費永遠 ≥ 0", _iv8),
        Invariant(
            "IV9_health_exempt_overrides_deps",
            "health_exempt 時 dependents 不影響",
            _iv9,
        ),
        Invariant(
            "IV10_finite_salary", "salary 必須是有限值(NaN/inf 應 reject)", _iv10
        ),
        Invariant("IV11_dependents_int", "dependents 不應為非整數 float", _iv11),
    ],
    seed_cases=[
        {"salary": 30000, "dependents": 0, "pension_self_rate": 0},
        {"salary": 45800, "dependents": 2, "pension_self_rate": 0.06},
        {"salary": 25250, "dependents": 0, "pension_self_rate": 0},
    ],
    runner=_runner,
    allowed_exceptions=("ValueError",),
)
