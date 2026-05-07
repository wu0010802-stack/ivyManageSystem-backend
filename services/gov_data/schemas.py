"""政府資料同步：內部資料模型（純 dataclass，不寫 DB）。

每個 source 的 parser 輸出對應的 *Result 類別，composer 讀這些類別合成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# 6 個 source key 的固定順序（fetcher / scheduler / UI 一致使用）
SOURCE_KEYS: list[str] = [
    "mol_labor_brackets",
    "mol_labor_premium",
    "mol_pension",
    "nhi_brackets",
    "nhi_premium",
    "mol_minimum_wage",
]


@dataclass(frozen=True)
class BracketRow:
    """合成後的 IvyKids 級距單列（對齊 insurance_brackets 表欄位）。"""

    amount: int
    labor_employee: int
    labor_employer: int
    health_employee: int
    health_employer: int
    pension: int

    def __post_init__(self):
        if self.amount <= 0:
            raise ValueError(f"amount must be > 0, got {self.amount}")
        for f in (
            "labor_employee",
            "labor_employer",
            "health_employee",
            "health_employer",
            "pension",
        ):
            if getattr(self, f) < 0:
                raise ValueError(f"{f} must be >= 0")


@dataclass
class LaborBracketsResult:
    """勞保投保薪資分級表 parser 輸出。"""

    effective_year: int
    amounts: list[int]  # 級距 amount 列表（已排序去重）
    max_insured: int  # 最高投保薪資


@dataclass
class LaborPremiumResult:
    """勞保保險費分擔表 parser 輸出。"""

    effective_year: int
    by_amount: dict[int, dict[str, int]]
    # by_amount[29500] = {"labor_employee": 738, "labor_employer": 2582}


@dataclass
class PensionResult:
    """勞退月提繳工資分級表。"""

    effective_year: int
    amounts: list[int]
    max_insured: int


@dataclass
class NhiBracketsResult:
    """健保投保金額分級表。"""

    effective_year: int
    amounts: list[int]
    max_insured: int


@dataclass
class NhiPremiumResult:
    """健保保險費負擔金額表（包含眷屬數）。

    Note: composer 只用 single.{employee, employer}（眷屬欄位僅供員工本人保費計算參考、
    IvyKids 級距 health_employer 不採加權；詳見 tests/fixtures/gov_data/_COMPOSER_JOIN_RULES.md）。
    """

    effective_year: int
    # by_amount[29500] = {
    #   "single": {"employee": 458, "employer": 1428},  # 0 眷屬
    #   "deps": {1: 916, 2: 1374, 3: 1832},             # 不同眷屬數，僅供參考
    # }
    by_amount: dict[int, dict]


@dataclass
class MinimumWageResult:
    """基本工資調整紀錄。"""

    history: list[tuple[date, int, int]]
    # [(effective_date, monthly, hourly), ...] 由舊到新

    def latest(self) -> Optional[tuple[date, int, int]]:
        return self.history[-1] if self.history else None


@dataclass
class ComposedBrackets:
    """composer 輸出：完整 IvyKids 級距版本。"""

    effective_year: int
    rows: list[BracketRow]  # 完整 N 列
    rates: dict  # InsuranceRate 對應變動欄位
    composed_from: dict[str, int]  # source -> snapshot_id
