"""services/salary/severance.py dead code guard。

2026-05-26 確認 severance.py 純函式無生產 caller（主 repo 唯一呼叫者是
tests/test_severance.py 自己）。本 guard 在 production caller 出現時 fail，
提醒：
    1. 補 endpoint spec（產品決策）
    2. 呼叫方需 utils.rounding.round_half_up
    3. severance 加入 .github/workflows/ci.yml money-rounding-gate paths
    4. 刪除本 guard test

Refs:
    - services/salary/severance.py module docstring
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_severance_has_no_production_callers():
    """偵測 calculate_severance_pay_* / calculate_service_years / calculate_average_monthly_wage
    出現在 services/ 或 api/ 即 fail（severance.py 自己除外）。

    若 fail，按 services/salary/severance.py module docstring 4 個步驟落實整合。
    """
    # git grep 比手寫 walk 快且自動 respect .gitignore；無 git 環境 fallback 略過
    try:
        result = subprocess.run(
            [
                "git",
                "grep",
                "-l",
                "-E",
                r"calculate_severance_pay_(new|old)|calculate_service_years|calculate_average_monthly_wage",
                "--",
                "services/**.py",
                "api/**.py",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        # 無 git command（CI/local 都不該發生，但 fail-soft）
        import pytest

        pytest.skip("git command unavailable")
        return

    matched_files = [
        line
        for line in result.stdout.strip().splitlines()
        if line and line != "services/salary/severance.py"
    ]

    assert not matched_files, (
        "services/salary/severance.py 出現新的 production caller："
        f"{matched_files}\n"
        "請按 services/salary/severance.py module docstring 落實 4 步整合："
        "(1) 補 endpoint spec (2) 呼叫方用 round_half_up (3) 加入 "
        "money-rounding-gate paths (4) 刪除本 guard test"
    )
