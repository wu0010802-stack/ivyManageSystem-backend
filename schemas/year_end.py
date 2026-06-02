"""年終獎金 API Pydantic schemas（M4 新增）。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from models.year_end import (
    SpecialBonusType,
    YearEndCycleStatus,
    YearEndSettlementStatus,
)

# ===== YearEndCycle =====


class YearEndCycleCreate(BaseModel):
    academic_year: int = Field(ge=100, le=200)
    start_date: date
    end_date: date
    bonus_calc_date: date


class YearEndCycleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    academic_year: int
    start_date: date
    end_date: date
    bonus_calc_date: date
    status: YearEndCycleStatus
    created_at: datetime


# ===== OrgYearSettings =====


class OrgYearSettingsCreate(BaseModel):
    semester_first: bool
    enrollment_target: int = 160
    enrollment_actual: Optional[int] = None
    school_achievement_rate: Decimal = Decimal("0")
    org_achievement_rate: Decimal
    meeting_absence_deduction: Decimal = Decimal("1000")


class OrgYearSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    year_end_cycle_id: int
    semester_first: bool
    enrollment_target: int
    enrollment_actual: Optional[int]
    school_achievement_rate: Decimal
    org_achievement_rate: Decimal
    meeting_absence_deduction: Decimal


# ===== ClassEnrollmentTarget =====


class ClassEnrollmentTargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    year_end_cycle_id: int
    semester_first: bool
    classroom_id: int
    head_teacher_employee_id: Optional[int]
    assistant_employee_id: Optional[int]
    head_count_target: int
    avg_monthly_enrollment: Decimal
    class_performance_rate: Decimal
    returning_student_rate: Decimal


# ===== Settlement =====


class SettlementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    year_end_cycle_id: int
    employee_id: int
    avg_performance_rate: Decimal
    base_salary: Decimal  # pii-allow: 年終結算基底（admin/hr 必看）
    festival_total: Decimal
    gross_amount: Decimal
    org_achievement_rate: Decimal
    subtotal_amount: Decimal
    deduction_total: Decimal
    hire_months: Decimal
    proration_rate: Decimal
    payable_amount: Decimal
    special_bonus_total: Decimal
    total_amount: Decimal
    remark: Optional[str]
    status: YearEndSettlementStatus
    version: int


# ===== SpecialBonusItem =====


class SpecialBonusItemCreate(BaseModel):
    employee_id: int
    bonus_type: SpecialBonusType
    period_label: str
    amount: Decimal
    classroom_id: Optional[int] = None
    calc_meta: dict[str, Any] = Field(default_factory=dict)
    source_ref: Optional[str] = None


class SpecialBonusItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    year_end_cycle_id: int
    employee_id: int
    bonus_type: SpecialBonusType
    period_label: str
    amount: Decimal
    classroom_id: Optional[int]
    calc_meta: dict[str, Any]
    source_ref: Optional[str]


# ===== Excel Import 結果 =====


class YearEndImportResultOut(BaseModel):
    cycle_id: int
    settlements_upserted: int
    special_bonuses_upserted: int
    class_targets_upserted: int
    skipped_unresolved_names: list[str]


# ===== 考核年終 Payout（Task 6）=====


class PayoutPreviewRow(BaseModel):
    employee_id: int
    employee_name: str
    role_group: str
    earlier_summary_id: Optional[int] = None
    earlier_amount: Decimal
    earlier_cycle_finalized: bool
    later_summary_id: Optional[int] = None
    later_amount: Decimal
    later_cycle_finalized: bool
    total_amount: Decimal
    is_inactive: bool
    warnings: list[str] = Field(default_factory=list)


class PayoutGenerateRequest(BaseModel):
    year: int = Field(..., ge=2024, le=2099)
    included_inactive_employee_ids: list[int] = Field(default_factory=list)


class PayoutGenerateResult(BaseModel):
    cycle_id: int
    generated_count: int
    affected_employee_count: int
    total_amount: Decimal
    skipped_inactive_count: int
    warnings: list[str] = Field(default_factory=list)


class PayoutItem(BaseModel):
    """已生成的 special_bonus_item 顯示 schema。"""

    id: int
    employee_id: int
    bonus_type: str
    period_label: str
    amount: Decimal
    source_ref: Optional[str] = None
    calc_meta: dict[str, Any]


# ===== Task 6: build / grid / manual-patch 端點 schema =====


class BuildSettlementsRequest(BaseModel):
    included_resigned_employee_ids: list[int] = []


class BuildResultOut(BaseModel):
    built: int
    skipped_finalized: int


class GridRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    employee_id: int
    employee_name: str
    payable_amount: Decimal
    special_bonuses: dict[str, Decimal]
    total_amount: Decimal
    status: str


class ManualPatchRequest(BaseModel):
    deduction_disciplinary: Optional[Decimal] = None
    excess_amount: Optional[Decimal] = None
    hire_months_override: Optional[Decimal] = None
