"""schemas/recruitment_timeline.py — 招生→學生 歷程時間軸事件。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


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
