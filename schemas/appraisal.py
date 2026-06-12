"""半年考核 API Pydantic schemas（M4 重建版）。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

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
    default_weight: Decimal  # pii-allow: 考核項目權重（非個人量測數據）
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
    bonus_amount: Decimal  # pii-allow: 考核獎金（admin 必看）
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


# ===== Aggregated Status (current-semester refactor) =====


class AttendanceAggregateOut(BaseModel):
    late_count: int
    early_leave_count: int
    missing_punch_count: int
    leave_days: int
    absent_days: int = Field(default=0, description="曠職天數（status='absent'）")
    suggested_score_delta: Decimal


class ClassRetentionAggregateOut(BaseModel):
    classroom_id: Optional[int] = None
    classroom_name: Optional[str] = None
    initial_count: int
    final_count: int
    retention_rate: Decimal
    suggested_score_delta: Decimal


class ActivityRateAggregateOut(BaseModel):
    classroom_id: Optional[int] = None
    grade_name: Optional[str] = Field(default=None, description="班級年級名")
    enrolled_students: int
    registered_for_activity: int
    activity_rate: Decimal
    suggested_score_delta: Decimal


class DisciplinaryActionItemOut(BaseModel):
    id: int
    action_date: date
    action_type: str
    deduction_amount: Optional[Decimal] = None
    reason: Optional[str] = None


class DisciplinaryAggregateOut(BaseModel):
    warning_count: int
    minor_count: int
    major_count: int
    commend_count: int = 0
    minor_merit_count: int = 0
    major_merit_count: int = 0
    actions: list[DisciplinaryActionItemOut] = Field(default_factory=list)
    suggested_score_delta: Decimal


class ParticipantStatusOut(BaseModel):
    participant_id: Optional[int] = None
    employee_id: int
    employee_name: str
    role_group: RoleGroup
    classroom_id: Optional[int] = None
    is_participant: bool = True
    hire_months_in_cycle: Optional[Decimal] = None
    reinstate_count: int = Field(default=0, description="復學事件數")
    attendance: AttendanceAggregateOut
    retention: ClassRetentionAggregateOut
    activity: ActivityRateAggregateOut
    disciplinary: DisciplinaryAggregateOut


class AggregatedStatusOut(BaseModel):
    cycle_id: int
    academic_year: int
    semester: Semester
    start_date: date
    end_date: date
    generated_at: datetime
    participants: list[ParticipantStatusOut]


class SyncResultPreviewItem(BaseModel):
    participant_id: int
    employee_name: str
    item_code: str
    old_score_delta: Decimal
    new_score_delta: Decimal
    source_ref: str


class SyncResultOut(BaseModel):
    cycle_id: int
    dry_run: bool
    deleted_count: int
    inserted_count: int
    skipped_manual_count: int
    items: list[SyncResultPreviewItem]


# ===== Bulk add participants from active employees =====


class BulkAddParticipantsRequest(BaseModel):
    employee_ids: Optional[list[int]] = None  # None = 全部在職員工


class BulkAddParticipantsResult(BaseModel):
    cycle_id: int
    created_count: int
    skipped_count: int
    created_participants: list[ParticipantOut]


# ===== Scoring Rules (calibrate Phase 1) =====


class PerUnitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    per_unit_delta: Decimal
    per_role_override: Optional[dict[str, Decimal]] = None
    unit_cap: Optional[int] = Field(default=None, gt=0)
    delta_cap: Optional[Decimal] = None


class TierItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: Decimal
    delta: Decimal


class TierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_field: str
    tiers: list[TierItem]

    @field_validator("tiers")
    @classmethod
    def must_have_min_zero(cls, v):
        if not v:
            raise ValueError("tiers 不可為空")
        if not any(t.min == 0 for t in v):
            raise ValueError("必須有一條 min=0 兜底")
        return v


class FlatThresholdConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_field: str
    threshold: Decimal
    above_delta: Decimal
    below_delta: Decimal


class DisciplinaryTieredConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warning_delta: Decimal
    minor_delta: Decimal
    major_delta: Decimal


class ManualDeltaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_delta: Decimal
    max_delta: Decimal

    @field_validator("max_delta")
    @classmethod
    def max_must_be_gte_min(cls, v, info):
        min_val = info.data.get("min_delta")
        if min_val is not None and v < min_val:
            raise ValueError("min_delta 不可大於 max_delta")
        return v


class ScoringRuleIn(BaseModel):
    item_code: str
    effective_from: date
    rule_type: Literal[
        "PER_UNIT", "TIER", "FLAT_THRESHOLD", "DISCIPLINARY_TIERED", "MANUAL_DELTA"
    ]
    rule_config: (
        dict  # 由 endpoint 內依 rule_type 二次 validate（用上方 4 config class）
    )
    applies_to_role_groups: Optional[list[str]] = None
    notes: Optional[str] = None


class ScoringRuleOut(ScoringRuleIn):
    id: int
    created_at: Optional[str] = None
    created_by: Optional[int] = None


# ===== Manual Event Counts (calibrate Phase 1) =====


class ManualEventCountIn(BaseModel):
    participant_id: int
    item_code: str
    # count 允許負值：MANUAL_DELTA 規則（如 CHILD_ACCIDENT）存分值本身（可負），
    # 其餘 rule_type 的事件次數仍須 ≥ 0，由 API handler 根據 rule_type 驗證。
    count: Decimal
    note: Optional[str] = None


class ManualEventCountBatchIn(BaseModel):
    entries: list[ManualEventCountIn]


class ManualEventCountOut(BaseModel):
    participant_id: int
    employee_name: str
    item_code: str
    count: Decimal
    entered_by: Optional[int]
    entered_at: Optional[str]


class ManualEventCountListOut(BaseModel):
    cycle_id: int
    entries: list[ManualEventCountOut]


# ===== Score Preview (calibrate Phase 1) =====


class ScorePreviewItem(BaseModel):
    item_code: str
    delta: Decimal
    raw_value: Decimal
    note: str
    current_db_value: Optional[Decimal] = None


class ScorePreviewParticipant(BaseModel):
    participant_id: int
    employee_name: str
    items: list[ScorePreviewItem]


class ScorePreviewOut(BaseModel):
    cycle_id: int
    on_date: date
    participants: list[ScorePreviewParticipant]


# ===== Signing UX：log / reject / comment / batch / status_summary =====


class SummaryLogOut(BaseModel):
    id: int
    summary_id: int
    action: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    actor_id: int
    actor_name: Optional[str] = None
    actor_role_snapshot: Optional[str] = None
    reason: Optional[str] = None
    comment: Optional[str] = None
    created_at: Optional[str] = None


class RejectIn(BaseModel):
    reason: str = Field(min_length=10)
    to_status: Optional[Literal["DRAFT", "SUPERVISOR_SIGNED", "ACCOUNTING_SIGNED"]] = (
        None
    )
    # null = 預設退一階；FINALIZED reject 用 ACCOUNTING_SIGNED


class CommentIn(BaseModel):
    comment: str = Field(min_length=1)


class BatchSignIn(BaseModel):
    summary_ids: list[int]
    stage: Literal["SUPERVISOR", "ACCOUNTING", "FINALIZE"]


class BatchSignErrorItem(BaseModel):
    summary_id: int
    error: str


class BatchSignResultOut(BaseModel):
    succeeded: list[int]
    failed: list[BatchSignErrorItem]


class SignStatusSummaryItem(BaseModel):
    id: int
    employee_id: int
    employee_name: str
    total_score: Decimal
    grade: str
    bonus_amount: Decimal
    updated_at: Optional[str] = None


class SignStatusBucket(BaseModel):
    status: str
    count: int
    summaries: list[SignStatusSummaryItem]


class SignStatusSummaryOut(BaseModel):
    cycle_id: int
    counts: dict[str, int]
    buckets: list[SignStatusBucket]
