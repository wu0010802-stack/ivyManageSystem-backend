"""Recruitment periods router (api/recruitment/periods.py) Out schemas。

Phase 3 範圍（本檔）：
- PeriodOut (對應 _period_to_dict)
- RecruitmentOptionsOut (GET /options)
- MonthOptionOut (GET /months / POST /months)
- MonthDeleteOut (DELETE /months/{month})

8 endpoint wired: GET/POST/PUT/sync /periods + /options + /months CRUD。

Out of scope (Phase 3.5)：
- GET /periods/summary (含 total/ratio/by_grade 多層 nested)
- DELETE /periods/{id} (status 204 no body)
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class PeriodOut(IvyBaseModel):
    """招生期間 — 對應 _period_to_dict shape。"""

    id: int
    period_name: str
    visit_count: int
    deposit_count: int
    enrolled_count: int
    transfer_term_count: int
    effective_deposit_count: int
    not_enrolled_deposit: int
    enrolled_after_school: int
    notes: Optional[str] = None
    sort_order: int
    visit_to_deposit_rate: float
    visit_to_enrolled_rate: float
    deposit_to_enrolled_rate: float
    effective_to_enrolled_rate: float
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class RecruitmentOptionsOut(IvyBaseModel):
    """GET /recruitment/options — 篩選用 dropdown 選項集合。"""

    months: list[str]
    grades: list[str]
    sources: list[str]
    referrers: list[str]
    no_deposit_reasons: list[str]


class MonthOptionOut(IvyBaseModel):
    """招生月份 — GET /months list 單筆 / POST /months 回傳。"""

    id: int
    month: str


class MonthDeleteOut(IvyBaseModel):
    """DELETE /months/{month} 回傳。"""

    deleted: str
