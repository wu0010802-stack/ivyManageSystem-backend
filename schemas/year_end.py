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

# 年終手動調整金額「量級」上限（pentest 2026-06-05 E1）：保留合法負值
# （FESTIVAL_DIFF 多退、disciplinary 獎懲），但擋荒謬注入（如 9,999,999）。
# 與薪資 manual_adjust 的 _MANUAL_ADJUST_FIELD_MAX(500k) 同精神，年終單列放寬至 100 萬。
_YEAR_END_AMOUNT_ABS_MAX = 1_000_000

# ===== YearEndCycle =====


class YearEndCycleCreate(BaseModel):
    academic_year: int = Field(ge=100, le=200)
    start_date: date
    end_date: date
    bonus_calc_date: date
    clone_from_academic_year: Optional[int] = None


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
    enrollment_target: int = Field(default=160, ge=0)
    enrollment_actual: Optional[int] = Field(default=None, ge=0)
    # 率欄位上限取 Numeric(6,3) 精度 999.999（達成率可合法 >100，勿封 100）；
    # 無 ge/le 時負數/超大值直灌年終 step1/step3（滲透測試 E1 同款，2026-06-11 補）。
    school_achievement_rate: Decimal = Field(
        default=Decimal("0"), ge=0, le=Decimal("999.999")
    )
    school_achievement_rate_override: Optional[Decimal] = Field(
        default=None, ge=0, le=Decimal("999.999")
    )
    org_achievement_rate: Decimal = Field(ge=0, le=Decimal("999.999"))
    meeting_absence_deduction: Decimal = Field(default=Decimal("1000"), ge=0)


class OrgYearSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    year_end_cycle_id: int
    semester_first: bool
    enrollment_target: int
    enrollment_actual: Optional[int]
    school_achievement_rate: Decimal
    school_achievement_rate_override: Optional[Decimal]
    effective_school_achievement_rate: Decimal
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
    # FESTIVAL_DIFF 可為負（多退）→ 對稱量級上限保留負值、擋荒謬注入（pentest E1）
    amount: Decimal = Field(ge=-_YEAR_END_AMOUNT_ABS_MAX, le=_YEAR_END_AMOUNT_ABS_MAX)
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
    # B8: derive_report 彙整欄位（供前端 F2 提醒用）
    # refresh_rates=False 時 derive_report 為 None，以 default 0/[] 回填
    unmatched_count: int = 0  # 才藝報名未配對班級筆數
    fallback_classes: int = 0  # 學號未回填沿用手填舊生率的班數
    warnings: list[str] = []  # 各 derive 警告串接（缺單價/缺班導/缺目標等）


class GridRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    settlement_id: int
    employee_id: int
    employee_name: str
    payable_amount: Decimal
    special_bonuses: dict[str, Decimal]
    total_amount: Decimal
    status: str


class ManualPatchRequest(BaseModel):
    # pentest E1：金額/月數邊界。disciplinary「獎懲」可負（大過 -6000）→ 對稱量級；
    # excess（超額編制獎金）無負值語意 → ge=0；hire_months 為在職月數 → 0~12。
    deduction_disciplinary: Optional[Decimal] = Field(
        default=None, ge=-_YEAR_END_AMOUNT_ABS_MAX, le=_YEAR_END_AMOUNT_ABS_MAX
    )
    excess_amount: Optional[Decimal] = Field(
        default=None, ge=0, le=_YEAR_END_AMOUNT_ABS_MAX
    )
    hire_months_override: Optional[Decimal] = Field(default=None, ge=0, le=12)


# ===== B1: class_targets upsert 請求 schema =====


class ClassEnrollmentTargetUpsert(BaseModel):
    semester_first: bool
    classroom_id: int
    head_teacher_employee_id: Optional[int] = None
    assistant_employee_id: Optional[int] = None
    head_count_target: int
    # pentest E3：舊生率為分數（×100 得百分比）→ 0~1
    returning_student_rate: Decimal = Field(default=Decimal("0"), ge=0, le=1)
