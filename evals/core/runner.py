"""Eval runner — 把 target、attacker 串起來,輸出 EvalReport。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

from .llm_attacker import Attacker
from .target import CaseResult, EvalReport, Target, run_one_case

logger = logging.getLogger(__name__)


def run_eval(
    target: Target,
    attacker: Attacker,
    *,
    n_cases: int = 20,
    run_seed_cases: bool = True,
) -> EvalReport:
    started = datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005
    logger.info(
        "[eval] target=%s attacker=%s n=%d", target.name, attacker.name, n_cases
    )

    seed_results: list[CaseResult] = []
    if run_seed_cases:
        for seed in target.seed_cases:
            r = run_one_case(target, seed)
            seed_results.append(r)
        n_violation = sum(1 for r in seed_results if r.has_violation)
        if n_violation:
            logger.warning(
                "[eval] seed 也有 %d 個違反!可能是 invariant 寫錯或 seed 不合法",
                n_violation,
            )

    cases = attacker.generate(target, n=n_cases)
    attack_results: list[CaseResult] = []
    for case in cases:
        # __hypothesis / __target_invariant 是 metadata,不傳進 runner
        meta = {
            k: case.pop(k) for k in ("__hypothesis", "__target_invariant") if k in case
        }
        r = run_one_case(target, case)
        if meta:
            r.outcome["_attacker_hypothesis"] = meta.get("__hypothesis")
            r.outcome["_attacker_target_invariant"] = meta.get("__target_invariant")
        attack_results.append(r)

    finished = datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005
    return EvalReport(
        target_name=target.name,
        attacker_name=attacker.name,
        total_cases=len(attack_results),
        seed_results=seed_results,
        attack_results=attack_results,
        started_at=started,
        finished_at=finished,
    )


def collect_violations(report: EvalReport) -> list[dict]:
    """攤平所有違反成 dict list,方便下一輪 attacker 學習。"""
    findings: list[dict] = []
    for src, results in (
        ("seed", report.seed_results),
        ("attack", report.attack_results),
    ):
        for r in results:
            if not r.has_violation:
                continue
            findings.append(
                {
                    "source": src,
                    "input": r.case_input,
                    "violations": r.violations,
                    "outcome_excerpt": _excerpt_outcome(r.outcome),
                }
            )
    return findings


def _excerpt_outcome(outcome: dict) -> dict:
    """裁剪 outcome 給 reporter / 下輪 prompt 用,避免巨大 result 撐爆 token。"""
    excerpt: dict = {"ok": outcome.get("ok")}
    if outcome.get("exception"):
        excerpt["exception"] = outcome["exception"]
    res = outcome.get("result")
    if res is not None:
        # 取前 500 字元代表
        text = repr(res)
        excerpt["result_repr"] = text[:500] + ("..." if len(text) > 500 else "")
    return excerpt
