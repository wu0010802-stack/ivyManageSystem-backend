"""半年考核 API Pydantic schemas（M4 重建版）。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

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
    academic_year: int = Field(ge=100, le=200)
    semester: Semester
    start_date: date
    end_date: date
    base_score_calc_date: date
    enrollment_target: Optional[int] = None
    enrollment_actual: Optional[int] = None


class CycleUpdate(BaseModel):
    base_score: Optional[Decimal] = None
    enrollment_target: Optional[int] = None
    enrollment_actual: Optional[int] = None
    status: Optional[CycleStatus] = None


class CycleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    academic_year: int
    semester: Semester
    start_date: date
    end_date: date
    base_score_calc_date: date
    base_score: Decimal
    enrollment_target: Optional[int]
    enrollment_actual: Optional[int]
    status: CycleStatus
    created_at: datetime


# ===== Participant =====


class ParticipantCreate(BaseModel):
    employee_id: int
    role_group: RoleGroup
    classroom_id: Optional[int] = None
    hire_months_in_cycle: Decimal = Decimal("6")
    is_excluded: bool = False
    exclude_reason: Optional[str] = None


class ParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    cycle_id: int
    employee_id: int
    role_group: RoleGroup
    classroom_id: Optional[int]
    hire_months_in_cycle: Decimal
    is_excluded: bool
    exclude_reason: Optional[str]


# ===== Score Item Catalog =====


class CatalogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str
    label: str
    sign: ScoreItemSign
    default_weight: Decimal
    data_source: Optional[str]
    description: Optional[str]
    display_order: int
    is_active: bool


# ===== Score Item =====


class ScoreItemCreate(BaseModel):
    item_code: str
    score_delta: Decimal
    sequence_no: int = 1
    raw_value: Optional[Decimal] = None
    note: Optional[str] = None


class ScoreItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    participant_id: int
    cycle_id: int
    item_code: str
    sequence_no: int
    score_delta: Decimal
    raw_value: Optional[Decimal]
    note: Optional[str]


# ===== Summary =====


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
    leave_note: Optional[str]
    status: SummaryStatus
    version: int


# ===== Bonus Rate =====


class BonusRateCreate(BaseModel):
    effective_from: date
    role_group: RoleGroup
    grade: Grade
    base_amount: Decimal


class BonusRateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    effective_from: date
    role_group: RoleGroup
    grade: Grade
    base_amount: Decimal


# ===== Excel Import 結果 =====


class ImportResultOut(BaseModel):
    cycle_id: int
    participants_created: int
    participants_updated: int
    score_items_upserted: int
    summaries_upserted: int
    skipped_unresolved_names: list[str]
