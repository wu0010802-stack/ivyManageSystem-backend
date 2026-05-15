"""半年考核 API 請求 / 回應 schema（Pydantic v2）。

對應 api/appraisal/* 各 router 端點。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models.appraisal import (
    CycleStatus,
    Grade,
    RoleGroup,
    ScoreItemSign,
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
    base_score: Optional[Decimal] = Field(default=None, ge=0, le=110)


class CycleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    academic_year: int
    semester: Semester
    start_date: date
    end_date: date
    base_score_calc_date: date
    base_score: Decimal
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
    base_score: Optional[Decimal] = Field(default=None, ge=0, le=110)
    target_enrollment: Optional[int] = Field(default=None, ge=0)
    actual_enrollment: Optional[int] = Field(default=None, ge=0)
    hire_months_in_cycle: Optional[Decimal] = Field(default=None, ge=0, le=6)


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
    hire_months_in_cycle: Decimal


# ===== Score Item =====


class ScoreItemUpsert(BaseModel):
    participant_id: int
    item_code: str = Field(min_length=1, max_length=40)
    score_delta: Decimal
    raw_value: Optional[Decimal] = None
    note: str = ""

    @field_validator("score_delta")
    @classmethod
    def _bounded(cls, v: Decimal) -> Decimal:
        if v < Decimal("-20") or v > Decimal("20"):
            raise ValueError("score_delta out of [-20, 20]")
        return v


class ScoreItemPatch(BaseModel):
    score_delta: Optional[Decimal] = None
    raw_value: Optional[Decimal] = None
    note: Optional[str] = None

    @field_validator("score_delta")
    @classmethod
    def _bounded(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is not None and (v < Decimal("-20") or v > Decimal("20")):
            raise ValueError("score_delta out of [-20, 20]")
        return v


class ScoreItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    participant_id: int
    item_code: str
    score_delta: Decimal
    raw_value: Optional[Decimal] = None
    note: str
    created_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime


# ===== Summary =====


class SignRequest(BaseModel):
    comment: str = Field(default="", max_length=500)


class FinalizeRequest(BaseModel):
    comment: str = Field(default="", max_length=500)
    reason: str = Field(min_length=4, max_length=200)


class RejectRequest(BaseModel):
    reason: str = Field(min_length=4, max_length=500)


class SummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    participant_id: int
    cycle_id: int
    base_score: Decimal
    item_score_sum: Decimal
    total_score: Decimal
    grade: Grade
    bonus_amount: Decimal
    leave_note: Optional[str] = None
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


# ===== Score Item Catalog =====


class CatalogPatch(BaseModel):
    label: Optional[str] = Field(default=None, max_length=80)
    default_weight: Optional[Decimal] = None
    data_source: Optional[str] = Field(default=None, max_length=40)
    is_active: Optional[bool] = None
    display_order: Optional[int] = None


class CatalogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    label: str
    sign: ScoreItemSign
    default_weight: Decimal
    data_source: str
    display_order: int
    is_active: bool
