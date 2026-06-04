"""Pydantic schemas for /recruitment/funnel."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Stage = Literal["visited", "deposited", "enrolled", "active"]


class FunnelCard(BaseModel):
    visit_id: int
    child_name: str
    grade: Optional[str]
    phone: Optional[str]
    district: Optional[str]
    source: Optional[str]
    deposited_at: Optional[datetime]
    student_id: Optional[int]
    current_stage: Stage
    provisional_grade_id: Optional[int] = None
    provisional_grade_name: Optional[str] = None
    target_school_year: Optional[int] = None


class FunnelSummary(BaseModel):
    visited_count: int
    deposited_count: int
    enrolled_count: int
    active_count: int


class FunnelBoardOut(BaseModel):
    stages: dict[Stage, list[FunnelCard]]
    summary: FunnelSummary


class TransitionIn(BaseModel):
    to_stage: Stage
    classroom_id: Optional[int] = None
    reason: Optional[str] = None


class TransitionOut(BaseModel):
    visit_id: int
    from_stage: Stage
    to_stage: Stage
    student_id: Optional[int]
    event_log_id: int
    warnings: list[str] = Field(default_factory=list)


# 歷程 schema 已搬到 recruitment_timeline；此處 re-export 保既有 import 不破
from schemas.recruitment_timeline import TimelineEvent, TimelineOut  # noqa: E402,F401
