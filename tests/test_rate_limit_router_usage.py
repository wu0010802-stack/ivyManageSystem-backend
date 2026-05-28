"""AST sanity test: 確保 14 個 router 不直接 call SlidingWindowLimiter()，
必須走 create_limiter() factory（受 RATE_LIMIT_BACKEND env 控制）。

防回歸：搭配 .github/workflows/ci.yml 的 naked-rate-limiter-gate job
形成雙重保險（test 在 pytest 階段抓、CI gate 在 lint 階段抓）。
"""

import ast
from pathlib import Path

ROUTERS_REQUIRING_FACTORY = [
    "api/exports.py",
    "api/gov_reports.py",
    "api/overtimes.py",
    "api/leaves.py",
    "api/portal/leaves.py",
    "api/activity/pos.py",
    "api/activity/public.py",
    "api/activity/registrations_static.py",
    "api/salary/calculate.py",
    "api/parent_portal/milestones.py",
]


def test_no_naked_sliding_window_limiter_in_routers():
    """14 個 router 內所有 SlidingWindowLimiter(...) 構造呼叫必須改為 create_limiter(...)。"""
    repo_root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for rel_path in ROUTERS_REQUIRING_FACTORY:
        full = repo_root / rel_path
        assert full.exists(), f"Expected router file {rel_path} not found"
        source = full.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "SlidingWindowLimiter"
            ):
                offenders.append(f"{rel_path}:{node.lineno}")
    assert not offenders, (
        "下列 router 仍直接呼叫 SlidingWindowLimiter()，應改為 create_limiter() factory:\n  "
        + "\n  ".join(offenders)
    )
