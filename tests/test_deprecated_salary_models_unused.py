"""守衛測試：已 DEPRECATED 的舊世代薪資設定 model 必須維持 0 runtime caller。

Why（系統設計審查 2026-06-14，資料模型維度）：models/salary.py 有 5 個被取代的
舊表（BonusSetting/InsuranceTable/DeductionRule/ClassBonusSetting/SalaryItem），
現役設定走 config.BonusConfig / config.InsuranceRate 等。這些死表命名與現役表
極易混淆（BonusSetting vs BonusConfig、InsuranceTable vs InsuranceRate），歷次
bug hunt 反覆得排除。

此測試鎖住「0 runtime caller」不變式：若有人在 api/services/utils 開始引用這些
DEPRECATED model，立即 fail，強迫做出決定（要嘛改用現役表、要嘛明確移除 deprecated
標記並重新評估）。掃描範圍排除 model 定義本身、tests、alembic migration。
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCAN_DIRS = ["api", "services", "utils"]

_DEPRECATED_MODELS = [
    "BonusSetting",
    "InsuranceTable",
    "DeductionRule",
    "ClassBonusSetting",
    "SalaryItem",
]


def test_deprecated_salary_models_have_no_runtime_callers() -> None:
    offenders: dict[str, list[str]] = {}
    for model in _DEPRECATED_MODELS:
        pattern = re.compile(rf"\b{model}\b")
        hits: list[str] = []
        for scan in _SCAN_DIRS:
            for path in (_ROOT / scan).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if pattern.search(line):
                        hits.append(f"{path.relative_to(_ROOT)}:{lineno}")
        if hits:
            offenders[model] = hits
    assert not offenders, (
        "下列 DEPRECATED 舊世代薪資設定 model 重新出現 runtime caller，請改用現役表"
        "（BonusConfig / InsuranceRate 等）或重新評估其 deprecated 狀態：\n"
        + "\n".join(f"  {m}: {locs}" for m, locs in offenders.items())
    )
