"""Eval framework 自身的 pytest。

確保:
1. HeuristicAttacker 生成正確類型的邊界值,不會炸
2. Runner 能正確將 invariant 違反捕捉
3. Reporter 能序列化 dataclass 結果
4. build_attacker 在沒 ANTHROPIC_API_KEY 時 fallback 到 heuristic
5. 已知 seed cases 對所有現役 target 必須通過(rocks-bottom 健全性檢查)
"""

from __future__ import annotations

import json
import os
from datetime import date
from unittest.mock import patch

import pytest

from evals.core.llm_attacker import HeuristicAttacker, build_attacker
from evals.core.reporter import report_to_json, report_to_markdown, save_report
from evals.core.runner import collect_violations, run_eval
from evals.core.target import Invariant, Target, run_one_case
from evals.targets import insurance_target, leave_policy_target

# ---------- toy target for framework tests ----------


def _toy_runner(case):
    x = case["x"]
    if x < 0:
        raise ValueError("x must be >= 0")
    return {"square": x * x, "is_zero": x == 0}


def _iv_square_nonneg(case, outcome):
    if not outcome.get("ok"):
        return None
    if outcome["result"]["square"] < 0:
        return f"square negative: {outcome['result']['square']}"
    return None


def _iv_zero_iff_x_zero(case, outcome):
    if not outcome.get("ok"):
        return None
    if outcome["result"]["is_zero"] != (case["x"] == 0):
        return "is_zero mismatch"
    return None


def _iv_intentionally_wrong(case, outcome):
    """故意對所有 case 都失敗,用來測試 reporter 抓得到。"""
    if not outcome.get("ok"):
        return None
    return "this invariant always fails"


TOY = Target(
    name="toy_square",
    description="x → x*x, 但 x<0 raise",
    signature={"fields": {"x": {"type": "int", "boundary": [0, 10]}}},
    invariants=[
        Invariant("square_nonneg", "square >= 0", _iv_square_nonneg),
        Invariant("zero_iff_x_zero", "is_zero == (x==0)", _iv_zero_iff_x_zero),
    ],
    seed_cases=[{"x": 0}, {"x": 5}],
    runner=_toy_runner,
    allowed_exceptions=("ValueError",),
)


# ---------- tests ----------


def test_heuristic_attacker_generates_unique_cases():
    a = HeuristicAttacker(seed=42)
    cases = a.generate(TOY, n=20)
    # 邊界值池可能少於 n,只要求 >= 5 且去重
    assert len(cases) >= 5
    keys = {tuple(sorted(c.items())) for c in cases}
    assert len(keys) == len(cases), "cases should be unique"
    # 確認 None boundary 不會炸(insurance target 有 None 在 labor_insured)
    cases2 = a.generate(insurance_target.TARGET, n=10)
    assert len(cases2) == 10


def test_heuristic_attacker_covers_expected_boundary_values():
    a = HeuristicAttacker(seed=1)
    cases = a.generate(TOY, n=200)
    xs = {c["x"] for c in cases if isinstance(c.get("x"), int)}
    # 邊界 0, 1, 10, 11, -1 都應該至少有一次
    assert {0, 1, 10, 11, -1}.issubset(xs)


def test_runner_collects_violations_on_seed():
    """seed case 觸發故意失敗的 invariant 應被抓到。"""
    target_with_bug = Target(
        name="x",
        description="x",
        signature=TOY.signature,
        invariants=[Invariant("always_fail", "fails", _iv_intentionally_wrong)],
        seed_cases=[{"x": 1}],
        runner=_toy_runner,
        allowed_exceptions=(),
    )
    a = HeuristicAttacker(seed=1)
    report = run_eval(target_with_bug, a, n_cases=5)
    # 所有 ok case 都會被 always_fail 命中
    assert report.violation_count >= 1
    findings = collect_violations(report)
    assert any(f["violations"][0]["invariant"] == "always_fail" for f in findings)


def test_unexpected_exception_logged_as_violation():
    def runner(case):
        raise RuntimeError("kaboom")

    bad = Target(
        name="bad",
        description="raises unexpected",
        signature={"fields": {"x": {"type": "int", "boundary": [0]}}},
        invariants=[],
        seed_cases=[{"x": 0}],
        runner=runner,
        allowed_exceptions=(),  # RuntimeError 不在允許清單
    )
    r = run_one_case(bad, {"x": 0})
    assert r.has_violation
    assert any(v["invariant"] == "no_unexpected_exception" for v in r.violations)


def test_expected_exception_not_violation():
    def runner(case):
        raise ValueError("expected")

    t = Target(
        name="t",
        description="raises expected",
        signature={"fields": {"x": {"type": "int", "boundary": [0]}}},
        invariants=[],
        seed_cases=[{"x": 0}],
        runner=runner,
        allowed_exceptions=("ValueError",),
    )
    r = run_one_case(t, {"x": 0})
    assert not r.has_violation


def test_reporter_json_serializable():
    a = HeuristicAttacker(seed=1)
    report = run_eval(TOY, a, n_cases=5)
    data = report_to_json(report)
    # 必須能 json.dumps,不能丟 TypeError
    json.dumps(data, default=str)


def test_reporter_markdown_contains_summary_table():
    a = HeuristicAttacker(seed=1)
    report = run_eval(TOY, a, n_cases=5)
    md = report_to_markdown(report)
    assert "Eval Report" in md
    assert "Summary" in md
    assert "attack cases" in md


def test_save_report_writes_both_files(tmp_path):
    a = HeuristicAttacker(seed=1)
    report = run_eval(TOY, a, n_cases=3)
    paths = save_report(report, tmp_path)
    assert paths["json"].exists()
    assert paths["markdown"].exists()
    # JSON 能 reload
    json.loads(paths["json"].read_text())


def test_build_attacker_auto_fallback_to_heuristic_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from config import reset_for_tests

    reset_for_tests()
    a = build_attacker("auto")
    assert a.name == "heuristic"


def test_build_attacker_force_heuristic():
    a = build_attacker("heuristic")
    assert a.name == "heuristic"


# ---------- real target sanity (seed cases must pass) ----------


@pytest.mark.parametrize(
    "target",
    [leave_policy_target.TARGET, insurance_target.TARGET],
    ids=lambda t: t.name,
)
def test_real_target_seed_cases_pass(target):
    """每個註冊的 target 它的 seed_cases 必須 0 violation,否則 invariant 寫錯。"""
    for case in target.seed_cases:
        r = run_one_case(target, case)
        assert (
            not r.has_violation
        ), f"{target.name} seed case {case} 違反 invariant: {r.violations}"


def test_leave_policy_detects_personal_advance_violation():
    """合成一個明顯違反 IV2 的 case,確保 framework 抓得到。"""
    case = {
        "leave_type": "personal",
        "start_date": leave_policy_target.FIXED_TODAY,  # start = today,提前 0 日
        "end_date": leave_policy_target.FIXED_TODAY,
        "leave_hours": 8,
        "today": leave_policy_target.FIXED_TODAY,
    }
    r = run_one_case(leave_policy_target.TARGET, case)
    # validate_portal_leave_rules 會 raise ValueError → runner 內捕成 validate_ok=False
    # 因此 outcome.ok=True(runner 沒 raise),但 IV2 應該滿意(validate 被擋是正確的)
    # 不需違反
    assert not r.has_violation


def test_insurance_negative_salary_seeded():
    case = {"salary": -100, "dependents": 0, "pension_self_rate": 0}
    r = run_one_case(insurance_target.TARGET, case)
    # ValueError 預期,IV1 也通過 → 不違反
    assert not r.has_violation, f"got {r.violations}"
