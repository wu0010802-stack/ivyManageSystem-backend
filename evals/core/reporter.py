"""把 EvalReport 序列化成 JSON 與 Markdown。"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .target import CaseResult, EvalReport


def report_to_json(report: EvalReport) -> dict:
    return {
        "target": report.target_name,
        "attacker": report.attacker_name,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "totals": {
            "seed_cases": len(report.seed_results),
            "attack_cases": report.total_cases,
            "violations": report.violation_count,
            "unexpected_exceptions": report.unexpected_exceptions,
        },
        "seed_results": [_case_to_dict(r) for r in report.seed_results],
        "attack_results": [_case_to_dict(r) for r in report.attack_results],
    }


def _case_to_dict(r: CaseResult) -> dict:
    return {
        "input": _safe(r.case_input),
        "outcome": _safe(r.outcome),
        "violations": r.violations,
    }


def _safe(obj):
    """讓 result(可能是 dataclass / Decimal / date)能 JSON 序列化。"""
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        pass
    if hasattr(obj, "__dataclass_fields__"):
        try:
            return asdict(obj)
        except Exception:
            pass
    if isinstance(obj, dict):
        return {str(k): _safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_safe(x) for x in obj]
    return repr(obj)


def report_to_markdown(report: EvalReport, *, max_examples: int = 20) -> str:
    lines: list[str] = []
    lines.append(f"# Eval Report: {report.target_name}")
    lines.append("")
    lines.append(f"- **Attacker**: `{report.attacker_name}`")
    lines.append(f"- **Started**: {report.started_at}")
    lines.append(f"- **Finished**: {report.finished_at}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"| metric | value |")
    lines.append(f"|---|---|")
    lines.append(f"| seed cases | {len(report.seed_results)} |")
    lines.append(f"| attack cases | {report.total_cases} |")
    lines.append(f"| total violations | {report.violation_count} |")
    lines.append(f"| unexpected exceptions | {report.unexpected_exceptions} |")
    lines.append("")

    seed_fail = [r for r in report.seed_results if r.has_violation]
    if seed_fail:
        lines.append("> ⚠ **Seed cases also failed.** Invariants may be miswritten,")
        lines.append(
            "> or seed inputs themselves are invalid. Review before trusting attack results."
        )
        lines.append("")

    attack_fail = [r for r in report.attack_results if r.has_violation]
    if not attack_fail and not seed_fail:
        lines.append("✅ No violations found.")
        return "\n".join(lines)

    lines.append("## Violations (attack)")
    lines.append("")
    grouped: dict[str, list[CaseResult]] = {}
    for r in attack_fail:
        for v in r.violations:
            grouped.setdefault(v["invariant"], []).append(r)

    for inv_name, results in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"### `{inv_name}` — {len(results)} failure(s)")
        lines.append("")
        for r in results[:max_examples]:
            reason = next(
                (v["reason"] for v in r.violations if v["invariant"] == inv_name),
                "(no reason)",
            )
            lines.append(f"- **input**: `{_safe(r.case_input)}`")
            lines.append(f"  - reason: {reason}")
            if r.outcome.get("exception"):
                lines.append(f"  - exception: `{r.outcome['exception']}`")
            elif r.outcome.get("result") is not None:
                excerpt = repr(r.outcome["result"])
                lines.append(
                    f"  - result: `{excerpt[:200]}{'...' if len(excerpt) > 200 else ''}`"
                )
            hyp = r.outcome.get("_attacker_hypothesis")
            if hyp:
                lines.append(f"  - attacker hypothesis: _{hyp}_")
        if len(results) > max_examples:
            lines.append(f"- … and {len(results) - max_examples} more")
        lines.append("")

    if seed_fail:
        lines.append("## Seed Failures")
        lines.append("")
        for r in seed_fail[:max_examples]:
            lines.append(f"- input: `{_safe(r.case_input)}`")
            for v in r.violations:
                lines.append(f"  - {v['invariant']}: {v['reason']}")
        lines.append("")

    return "\n".join(lines)


def save_report(report: EvalReport, dir_path: str | Path) -> dict[str, Path]:
    """寫 JSON + Markdown 到 dir_path,回傳路徑 dict。"""
    p = Path(dir_path)
    p.mkdir(parents=True, exist_ok=True)
    stem = (
        f"{report.target_name}__{report.attacker_name}"
        f"__{report.started_at.replace(':', '-')}"
    )
    json_path = p / f"{stem}.json"
    md_path = p / f"{stem}.md"
    json_path.write_text(
        json.dumps(report_to_json(report), ensure_ascii=False, indent=2, default=str)
    )
    md_path.write_text(report_to_markdown(report))
    return {"json": json_path, "markdown": md_path}
