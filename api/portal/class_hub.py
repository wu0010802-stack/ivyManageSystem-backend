"""教師工作台彙整 endpoint。"""

import logging
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import get_session
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


# ============ Pydantic Models ============


class ClassHubTaskSample(BaseModel):
    student_id: int
    student_name: str
    detail: Optional[str] = None
    due_at: Optional[datetime] = None


class ClassHubTask(BaseModel):
    kind: Literal[
        "attendance_in",
        "attendance_out",
        "medication",
        "observation",
        "incident",
        "contact_book",
    ]
    count: int
    due_at: Optional[datetime] = None
    samples: list[ClassHubTaskSample] = []
    action_mode: Literal["sheet", "page", "inline_button"]


class ClassHubSlotData(BaseModel):
    slot_id: Literal["morning", "forenoon", "noon", "afternoon"]
    tasks: list[ClassHubTask] = []


class ClassHubCounts(BaseModel):
    attendance_check_in_pending: int = 0
    attendance_check_out_pending: int = 0
    medications_pending: int = 0
    observations_pending: int = 0
    incidents_today: int = 0
    contact_books_pending: int = 0


class ClassHubStickyTask(BaseModel):
    kind: str
    student_name: Optional[str] = None
    detail: str
    due_at: datetime
    deep_link: str


class ClassHubTodayResponse(BaseModel):
    classroom_id: int
    classroom_name: str
    fetched_at: datetime
    sticky_next: Optional[ClassHubStickyTask] = None
    weekly_assessment_due: int = 0
    counts: ClassHubCounts
    slots: list[ClassHubSlotData]
