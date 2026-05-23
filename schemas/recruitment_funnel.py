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


class TimelineEvent(BaseModel):
    source: Literal["recruitment", "student"]
    event_type: str
    from_stage: Optional[str]
    to_stage: Optional[str]
    actor_user_id: Optional[int]
    reason: Optional[str]
    created_at: datetime


class TimelineOut(BaseModel):
    events: list[TimelineEvent]
