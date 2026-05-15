"""CLI 入口:python -m evals.cli run <target> [--mode auto|llm|heuristic] [-n 50]

Example:
    python -m evals.cli list
    python -m evals.cli run insurance_service -n 100
    python -m evals.cli run leave_policy --mode heuristic -n 50
"""

from __future__ import annotations

import argparse
import importlib
import logging
import pkgutil
import sys
from pathlib import Path

from .core.llm_attacker import build_attacker
from .core.reporter import save_report
from .core.runner import run_eval
from .core.target import Target

logger = logging.getLogger("evals")

REPORTS_DIR = Path(__file__).parent / "reports"


def discover_targets() -> dict[str, Target]:
    """掃 evals.targets 套件,找出每個 module 的 TARGET 物件。"""
    targets: dict[str, Target] = {}
    pkg = importlib.import_module("evals.targets")
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.ispkg or info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"evals.targets.{info.name}")
        t = getattr(mod, "TARGET", None)
        if isinstance(t, Target):
            targets[t.name] = t
    return targets


def cmd_list(args: argparse.Namespace) -> int:
    targets = discover_targets()
    if not targets:
        print("(no targets registered)")
        return 0
    print(f"{'name':24s}  {'invariants':>10}  description")
    print("-" * 80)
    for t in targets.values():
        print(f"{t.name:24s}  {len(t.invariants):>10}  {t.description[:60]}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    targets = discover_targets()
    if args.target not in targets:
        print(f"未知 target: {args.target}", file=sys.stderr)
        print(f"可用: {', '.join(targets)}", file=sys.stderr)
        return 2
    target = targets[args.target]
    attacker = build_attacker(args.mode)
    report = run_eval(target, attacker, n_cases=args.n)
    paths = save_report(report, REPORTS_DIR)

    print()
    print(f"=== {target.name} (attacker={attacker.name}) ===")
    print(
        f"seed={len(report.seed_results)} "
        f"attack={report.total_cases} "
        f"violations={report.violation_count} "
        f"unexpected_exc={report.unexpected_exceptions}"
    )
    print(f"JSON:     {paths['json']}")
    print(f"Markdown: {paths['markdown']}")
    return 1 if report.violation_count else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(
        prog="evals", description="對抗測試 eval framework"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有 target")

    p_run = sub.add_parser("run", help="跑一個 target")
    p_run.add_argument("target", help="target 名稱(見 list)")
    p_run.add_argument(
        "--mode",
        choices=["auto", "llm", "heuristic", "offline-claude"],
        default="auto",
        help=(
            "attacker 模式;auto=有 ANTHROPIC_API_KEY 走 llm 否則 heuristic;"
            "offline-claude=讀預先生成的 Claude case 庫(無需 API)"
        ),
    )
    p_run.add_argument("-n", type=int, default=50, help="生成 case 數")

    args = parser.parse_args(argv)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "run":
        return cmd_run(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
