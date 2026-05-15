"""年終獎金 API 請求 / 回應 schema（Pydantic v2）。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from models.appraisal import RoleGroup
from models.year_end import (
    SettlementStatus,
    SpecialBonusType,
    YearEndCycleStatus,
)


# ===== Cycle =====


class YearEndCycleCreate(BaseModel):
    academic_year: int = Field(ge=100, le=200)


class YearEndCycleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    academic_year: int
    status: YearEndCycleStatus
    params_snapshot: dict[str, Any]
    created_at: datetime


# ===== Org Settings =====


class OrgSettingsUpsert(BaseModel):
    total_enrollment_target: int = Field(ge=0)
    achievement_rate_first: Decimal = Field(ge=0)
    achievement_rate_second: Decimal = Field(ge=0)
    org_achievement_rate: Decimal = Field(ge=0)
    festival_bonus_total_amount: Decimal = Field(ge=0, default=Decimal("0"))
    org_meeting_deduction: Decimal = Field(ge=0, default=Decimal("0"))
    extras_json: dict[str, Any] = Field(default_factory=dict)


class OrgSettingsOut(OrgSettingsUpsert):
    model_config = ConfigDict(from_attributes=True)
    id: int
    cycle_id: int


# ===== Class Target =====


class ClassTargetUpsert(BaseModel):
    classroom_id: int
    staffing_target: int = Field(ge=0)
    achievement_rate_first: Decimal = Field(ge=0)
    achievement_rate_second: Decimal = Field(ge=0)
    returning_rate_first: Decimal = Field(default=Decimal("0"), ge=0)
    returning_rate_second: Decimal = Field(default=Decimal("0"), ge=0)


class ClassTargetOut(ClassTargetUpsert):
    model_config = ConfigDict(from_attributes=True)
    id: int
    cycle_id: int


# ===== Employee Snapshot =====


class EmployeeSnapshotUpsert(BaseModel):
    employee_id: int
    base_salary: Decimal = Field(ge=0)
    festival_total: Decimal = Field(ge=0)
    role_group: RoleGroup
    hire_date: Optional[date] = None
    classroom_id: Optional[int] = None
    is_resigned: bool = False
    resign_date: Optional[date] = None
    is_contracted: bool = True


class EmployeeSnapshotOut(EmployeeSnapshotUpsert):
    model_config = ConfigDict(from_attributes=True)
    id: int
    cycle_id: int
    captured_at: datetime


# ===== Settlement =====


class SettlementDeductionsInput(BaseModel):
    late: Decimal = Field(default=Decimal("0"), ge=0)
    personal_leave: Decimal = Field(default=Decimal("0"), ge=0)
    sick_leave: Decimal = Field(default=Decimal("0"), ge=0)
    meeting: Decimal = Field(default=Decimal("0"), ge=0)
    disciplinary: Decimal = Field(default=Decimal("0"), ge=0)
    parental_leave: Decimal = Field(default=Decimal("0"), ge=0)


class SettlementCalculate(BaseModel):
    snapshot_id: int
    deductions: SettlementDeductionsInput = SettlementDeductionsInput()


class SettlementFinalize(BaseModel):
    reason: str = Field(min_length=4, max_length=200)


class SettlementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    cycle_id: int
    snapshot_id: int
    employee_id: int
    avg_performance_rate: Decimal
    gross_amount: Decimal
    subtotal_amount: Decimal
    deduction_total: Decimal
    deduction_late: Decimal
    deduction_personal_leave: Decimal
    deduction_sick_leave: Decimal
    deduction_meeting: Decimal
    deduction_disciplinary: Decimal
    deduction_parental_leave: Decimal
    payable_subtotal: Decimal
    special_bonus_sum: Decimal
    total_amount: Decimal
    calc_meta: dict[str, Any]
    status: SettlementStatus
    calculated_at: Optional[datetime] = None
    finalized_at: Optional[datetime] = None


# ===== Special Bonus =====


class SpecialBonusUpsert(BaseModel):
    employee_id: int
    bonus_type: SpecialBonusType
    period_label: str = Field(default="", max_length=20)
    amount: Decimal
    calc_meta: dict[str, Any] = Field(default_factory=dict)


class SpecialBonusOut(SpecialBonusUpsert):
    model_config = ConfigDict(from_attributes=True)
    id: int
    cycle_id: int
    created_at: datetime
