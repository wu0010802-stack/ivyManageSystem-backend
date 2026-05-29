"""schemas/student_lifecycle.py — 在校歷程 read-only output schema。"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel


class StepOut(BaseModel):
    key: Literal["visited", "deposited", "enrolled", "active", "terminal"]
    label: str
    status: Literal["done", "current", "future"]
    occurred_at: Optional[date] = None
    meta: Optional[dict] = None


class GradeStepOut(BaseModel):
    grade_id: int
    name: str
    sort_order: int
    status: Literal["done", "current", "future", "skipped"]
    entered_at: Optional[date] = None
    expected_at: Optional[date] = None
    classroom_name: Optional[str] = None


class TerminalOut(BaseModel):
    kind: Literal["graduated", "withdrawn", "transferred", "none"]
    actual_date: Optional[date] = None
    expected_date: Optional[date] = None


class LifecycleOverviewOut(BaseModel):
    student_id: int
    current_stage: str
    on_leave_badge: bool
    on_leave_since: Optional[date] = None
    outer_steps: list[StepOut]
    inner_grade_steps: list[GradeStepOut]
    terminal: TerminalOut
