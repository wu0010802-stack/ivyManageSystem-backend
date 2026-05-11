"""教職員考核（appraisal）API 請求 / 回應 schema（Pydantic v2）。

對應 api/appraisal/* 各 router 端點。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models.appraisal import (
    CatalogCategory,
    CycleStatus,
    EventType,
    Grade,
    ParentReaction,
    RoleGroup,
    Semester,
    SummaryStatus,
)

# ===== Cycle =====


class CycleCreate(BaseModel):
    academic_year: int = Field(ge=100, le=200)  # 民國年制
    semester: Semester
    base_score_calc_date: Optional[date] = None  # 預設 9/15 或 3/15


class CyclePatch(BaseModel):
    base_score_calc_date: Optional[date] = None


class CycleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    academic_year: int
    semester: Semester
    start_date: date
    end_date: date
    base_score_calc_date: date
    status: CycleStatus
    created_at: datetime


class CycleUnlockRequest(BaseModel):
    reason: str = Field(min_length=4, max_length=200)


# ===== Participant =====


class ParticipantBulkInit(BaseModel):
    employee_ids: Optional[list[int]] = None  # None = 在職全帶


class ParticipantPatch(BaseModel):
    role_group: Optional[RoleGroup] = None
    classroom_id: Optional[int] = None
    base_score: Optional[Decimal] = None
    target_enrollment: Optional[int] = None
    actual_enrollment: Optional[int] = None


class ParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    cycle_id: int
    employee_id: int
    role_group: RoleGroup
    classroom_id: Optional[int] = None
    base_score: Decimal
    target_enrollment: Optional[int] = None
    actual_enrollment: Optional[int] = None


# ===== Event =====


class EventCreate(BaseModel):
    participant_id: int
    catalog_item_id: Optional[int] = None
    event_type: EventType
    event_date: date
    score_delta: Decimal
    severity_level: Optional[int] = Field(default=None, ge=1, le=5)
    parent_reaction: Optional[ParentReaction] = None
    title: str = Field(min_length=1, max_length=120)
    detail: str = ""

    @field_validator("score_delta")
    @classmethod
    def _bounded(cls, v: Decimal) -> Decimal:
        if v < Decimal("-20") or v > Decimal("20"):
            raise ValueError("score_delta out of [-20, 20]")
        return v


class EventPatch(BaseModel):
    event_type: Optional[EventType] = None
    event_date: Optional[date] = None
    score_delta: Optional[Decimal] = None
    severity_level: Optional[int] = Field(default=None, ge=1, le=5)
    parent_reaction: Optional[ParentReaction] = None
    title: Optional[str] = Field(default=None, max_length=120)
    detail: Optional[str] = None

    @field_validator("score_delta")
    @classmethod
    def _bounded(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is not None and (v < Decimal("-20") or v > Decimal("20")):
            raise ValueError("score_delta out of [-20, 20]")
        return v


class EventRevert(BaseModel):
    # min_length=2 讓 2 字繁體中文（如「誤登」）可通過；空字串仍被擋
    reason: str = Field(min_length=2, max_length=200)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    participant_id: int
    cycle_id: int
    catalog_item_id: Optional[int] = None
    event_type: EventType
    event_date: date
    score_delta: Decimal
    severity_level: Optional[int] = None
    parent_reaction: Optional[ParentReaction] = None
    title: str
    detail: str
    attachments: list[dict[str, Any]]
    created_by: int
    created_at: datetime
    reverted_at: Optional[datetime] = None


# ===== Summary =====


class SignRequest(BaseModel):
    comment: str = Field(default="", max_length=500)


class FinalizeRequest(BaseModel):
    comment: str = Field(default="", max_length=500)
    reason: str = Field(min_length=4, max_length=200)  # 第三階雙簽 reason


class RejectRequest(BaseModel):
    reason: str = Field(min_length=4, max_length=500)


class SummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    participant_id: int
    cycle_id: int
    base_score: Decimal
    event_score_sum: Decimal
    total_score: Decimal
    grade: Grade
    bonus_amount: Decimal
    status: SummaryStatus
    supervisor_signed_at: Optional[datetime] = None
    supervisor_comment: Optional[str] = None
    accounting_signed_at: Optional[datetime] = None
    accounting_comment: Optional[str] = None
    finalized_at: Optional[datetime] = None
    finalized_comment: Optional[str] = None
    version: int


# ===== Bonus Rate =====


class BonusRateCreate(BaseModel):
    effective_from: date
    role_group: RoleGroup
    grade: Grade
    base_amount: Decimal = Field(ge=0)


class BonusRateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    effective_from: date
    role_group: RoleGroup
    grade: Grade
    base_amount: Decimal


# ===== Penalty Catalog =====


class CatalogPatch(BaseModel):
    default_score_delta: Optional[Decimal] = None
    severity_max: Optional[int] = Field(default=None, ge=1, le=5)
    is_active: Optional[bool] = None
    display_order: Optional[int] = None


class CatalogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    category: CatalogCategory
    subcategory: str
    description: str
    default_event_type: EventType
    default_score_delta: Decimal
    severity_max: int
    display_order: int
    is_active: bool
