"""seedgen 執行期共享 registry。

`SeedContext` 是所有 domain 模組讀寫的「唯一介面」:持有 session / config / RNG,
以及各階段已建實體的 registry(class_grades / job_titles / employees /
classrooms / users / students / guardians ...)。模組只透過 ctx 取依賴,
不重查已建實體;寫完一律 `ctx.log(table, n)` 累加筆數。

月份相關判定(`closed_months` / `current_month`)委派 `calendar.py`,
以 `config.today` / `config.year_start` 為基準。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .calendar import closed_months, current_month, current_term
from .config import SeedConfig

if TYPE_CHECKING:  # pragma: no cover - 僅供型別檢查,避免 runtime import 業務 model
    from sqlalchemy.orm import Session


@dataclass
class SeedContext:
    """seedgen 各模組共享的執行期狀態與已建實體 registry。

    欄位(對齊計畫「共享契約」,不可漂移):
        session: 當前 DB session(由 orchestrator 注入;單元測試可傳 None)。
        config: 本次執行的 SeedConfig。
        rng: 決定論隨機來源(random.Random)。
        class_grades: 已建年級清單。
        job_titles: username/key → JobTitle 對照(以職稱 key 索引)。
        employees: 已建員工清單。
        employees_by_role: role key → Employee list
            (supervisor/admin/accountant/homeroom/assistant/art/support)。
        classrooms: 已建班級清單。
        users: username → User 對照。
        students: 全體學生清單。
        students_active: 在籍(active)學生清單。
        guardians: 已建監護人清單。
        counts: table → 已建筆數累計(由 log 維護)。
    """

    session: "Session | None"
    config: SeedConfig
    rng: random.Random
    class_grades: list[Any] = field(default_factory=list)
    job_titles: dict[str, Any] = field(default_factory=dict)
    employees: list[Any] = field(default_factory=list)
    employees_by_role: dict[str, list[Any]] = field(default_factory=dict)
    classrooms: list[Any] = field(default_factory=list)
    users: dict[str, Any] = field(default_factory=dict)
    students: list[Any] = field(default_factory=list)
    students_active: list[Any] = field(default_factory=list)
    guardians: list[Any] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def log(self, table: str, n: int) -> None:
        """累加 `table` 已建筆數(同一 table 多次呼叫會累加)。"""
        self.counts[table] = self.counts.get(table, 0) + n

    def closed_months(self) -> list[tuple[int, int]]:
        """委派 calendar:回傳自學年起日至 today 為止已封存的 (year, month)。"""
        return closed_months(self.config.year_start, self.config.today)

    def current_month(self) -> tuple[int, int]:
        """委派 calendar:回傳 today 所在的 (year, month)。"""
        return current_month(self.config.today)

    def current_term(self) -> tuple[int, int]:
        """委派 calendar:回傳 today 所在的 (school_year 民國, semester)。

        term-container(班級/才藝課程/報名)以此 tag,才會落在 app「當前學期」過濾內。
        """
        return current_term(self.config.today)
