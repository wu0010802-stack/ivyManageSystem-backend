"""Target / Invariant / EvalCase 介面。

Target 是要被攻擊的被測單位。它聲明:
- name: 唯一識別
- signature: 用 JSON schema 風格描述 input 欄位(供 attacker 生成)
- invariants: 一組必須恆真的性質(對 result 做檢查)
- seed_cases: 已知有效的 case(讓 attacker 學介面,也作為 sanity test)
- runner: callable(case_dict) -> {"ok": bool, "result": Any, "exception": str|None}
- allowed_exceptions: 哪些 exception 是預期的(invariants 不檢查)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class Invariant:
    """單一不變量。

    check(case_input, outcome) -> Optional[str]
        回傳 None 表示通過;回傳字串表示違反原因。
        outcome 是 {"ok": bool, "result": Any, "exception": str|None}
    """

    name: str
    description: str
    check: Callable[[dict, dict], Optional[str]]


@dataclass
class Target:
    name: str
    description: str
    signature: dict  # JSON Schema-like; 給 attacker 看的介面契約
    invariants: list[Invariant]
    seed_cases: list[dict]
    runner: Callable[[dict], dict]
    allowed_exceptions: tuple[str, ...] = ()


@dataclass
class CaseResult:
    case_input: dict
    outcome: dict  # {"ok": bool, "result": Any, "exception": str|None, "traceback": str|None}
    violations: list[dict] = field(default_factory=list)
    # violation: {"invariant": name, "reason": str}

    @property
    def passed(self) -> bool:
        return (
            not self.violations
            and self.outcome.get("ok", False) is not False
            or (self.outcome.get("exception") and not self.violations)
        )

    @property
    def has_violation(self) -> bool:
        return bool(self.violations)


@dataclass
class EvalReport:
    target_name: str
    attacker_name: str
    total_cases: int
    seed_results: list[CaseResult]
    attack_results: list[CaseResult]
    started_at: str
    finished_at: str

    @property
    def violation_count(self) -> int:
        return sum(1 for r in self.attack_results if r.has_violation) + sum(
            1 for r in self.seed_results if r.has_violation
        )

    @property
    def unexpected_exceptions(self) -> int:
        return sum(
            1
            for r in self.attack_results
            if r.outcome.get("exception") and not r.outcome.get("expected_exception")
        )


def run_one_case(target: Target, case_input: dict) -> CaseResult:
    """執行一個 case 並評估所有 invariants。"""
    import traceback as tb

    outcome: dict
    try:
        result = target.runner(case_input)
        outcome = {"ok": True, "result": result, "exception": None, "traceback": None}
    except Exception as exc:  # noqa: BLE001 - 我們刻意接所有 exception
        exc_name = type(exc).__name__
        outcome = {
            "ok": False,
            "result": None,
            "exception": f"{exc_name}: {exc}",
            "traceback": tb.format_exc(limit=4),
            "expected_exception": exc_name in target.allowed_exceptions,
        }

    violations: list[dict] = []
    for inv in target.invariants:
        try:
            reason = inv.check(case_input, outcome)
        except Exception as exc:  # noqa: BLE001
            # invariant 自身出 bug,當作違反 + 警示
            reason = f"invariant raised {type(exc).__name__}: {exc}"
        if reason:
            violations.append({"invariant": inv.name, "reason": reason})

    # 未預期的 exception 視為「結構違反」
    if outcome.get("exception") and not outcome.get("expected_exception"):
        violations.append(
            {
                "invariant": "no_unexpected_exception",
                "reason": outcome["exception"],
            }
        )

    return CaseResult(case_input=case_input, outcome=outcome, violations=violations)
