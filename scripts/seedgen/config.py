"""seedgen 參數設定。

`SeedConfig` 為 frozen dataclass，收斂測試資料產生器的所有可調參數，
並衍生學年起訖日期與規模 profile。所有欄位與 property 對齊計畫
「共享契約」，不可漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# 各規模對應的班級/學生/員工數量。standard 為預設正式規模，
# small 供快速冒煙，large 供壓力測試。
_SCALE_PROFILES: dict[str, dict[str, int]] = {
    "standard": {"classrooms": 7, "students": 170, "employees": 23},
    "small": {"classrooms": 3, "students": 60, "employees": 12},
    "large": {"classrooms": 12, "students": 420, "employees": 42},
}


@dataclass(frozen=True)
class SeedConfig:
    """seedgen 的不可變設定。

    欄位:
        academic_year: 民國學年(114 → 2025-08-01 ~ 2026-07-31)。
        today: 模擬「現在」的日期,決定哪些月份已封存/進行中/未來。
        scale: 規模 profile key(standard/small/large)。
        rng_seed: 決定論隨機種子。
        wipe: 是否清除既有業務資料(由 CLI 控制)。
        confirm: 是否已確認(--yes)。
        allow_non_dev: 是否略過 dev DB 護欄(--i-know-not-dev)。
        only: 只跑指定模組(--only),空 tuple 代表全跑。
    """

    academic_year: int = 114
    today: date = date(2026, 2, 16)
    scale: str = "standard"
    rng_seed: int = 20260614
    wipe: bool = False
    confirm: bool = False
    allow_non_dev: bool = False
    only: tuple[str, ...] = field(default_factory=tuple)

    @property
    def year_start(self) -> date:
        """學年起日:民國 academic_year 對應西元 +1911 的 8/1。"""
        return date(self.academic_year + 1911, 8, 1)

    @property
    def year_end(self) -> date:
        """學年訖日:隔年 7/31(+1912)。"""
        return date(self.academic_year + 1912, 7, 31)

    @property
    def scale_profile(self) -> dict[str, int]:
        """回傳當前 scale 對應的數量 profile dict。"""
        return _SCALE_PROFILES[self.scale]
