"""政府開放資料同步：抓取 → 解析 → 合成 → 待審核 → promote 流程。

使用方式：
- 排程：services.gov_data_scheduler 內每 24h 觸發 fetch_all()
- 手動：api.gov_data_sync 收到 sync-now 觸發 fetch_all()
- 審核：api.gov_data_sync 提供 promote/dismiss endpoint，呼叫 promoter 模組
"""

from services.gov_data.schemas import (
    BracketRow,
    ComposedBrackets,
    LaborBracketsResult,
    LaborPremiumResult,
    MinimumWageResult,
    NhiBracketsResult,
    NhiPremiumResult,
    PensionResult,
    SOURCE_KEYS,
)
from services.gov_data.utils import compute_brackets_diff, sha256_of_payload

__all__ = [
    "BracketRow",
    "ComposedBrackets",
    "LaborBracketsResult",
    "LaborPremiumResult",
    "MinimumWageResult",
    "NhiBracketsResult",
    "NhiPremiumResult",
    "PensionResult",
    "SOURCE_KEYS",
    "compute_brackets_diff",
    "sha256_of_payload",
]
