"""PR2 收尾驗證：services/+api/+models/+utils/+scripts/+evals/ 內無任何 raw
datetime.now() / datetime.utcnow() / date.today() / datetime.today() 殘留。

PR1 已為這些位置加 `# noqa: DTZxxx` 暫留標記；PR2 機械替換後該標記應全部
消失，原 call site 改用 utils.taipei_time helper。

此測試在 PR2 替換完成前會 FAIL（grep 命中所有 noqa 行）；
PR2 Task 12-14 替換完畢後 PASS。
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIRS = ["services/", "api/", "models/", "utils/", "scripts/", "evals/"]
FORBIDDEN_PATTERNS = [
    r"\bdatetime\.now\(\)",
    r"\bdatetime\.utcnow\(\)",
    r"\bdate\.today\(\)",
    r"\bdatetime\.today\(\)",
]
# utils/taipei_time.py 為唯一合法入口
ALLOWED_FILE_SUFFIX = "utils/taipei_time.py"


def _grep_runtime_paths(pattern: str) -> list[str]:
    """Return matching file:line lines, excluding the allowed helper file."""
    result = subprocess.run(
        ["grep", "-rEn", pattern, *RUNTIME_DIRS, "--include=*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    lines = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        if ALLOWED_FILE_SUFFIX in line:
            continue
        lines.append(line)
    return lines


def test_no_raw_datetime_in_runtime_paths():
    """Aggregate check: 4 個 forbidden pattern 在 6 個 runtime 目錄都應 0 命中。"""
    all_violations = []
    for pattern in FORBIDDEN_PATTERNS:
        hits = _grep_runtime_paths(pattern)
        if hits:
            all_violations.append(f"\n=== Pattern: {pattern} ({len(hits)} hits) ===")
            all_violations.extend(hits[:5])  # 前 5 行樣本，避免 output 爆炸
            if len(hits) > 5:
                all_violations.append(f"... and {len(hits) - 5} more")

    assert not all_violations, (
        "Raw datetime/date 呼叫殘留於 runtime path；請改用 "
        "utils.taipei_time.now_taipei_naive() / now_taipei_aware() / today_taipei():\n"
        + "\n".join(all_violations)
    )
