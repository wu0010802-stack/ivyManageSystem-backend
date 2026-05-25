"""Pydantic schema for offboarding endpoints."""

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class OffboardingPreviewRequest(BaseModel):
    resign_date: date
    resign_reason: Optional[str] = None


class OffboardingProcessRequest(BaseModel):
    resign_date: date
    resign_reason: Optional[str] = None


class LeaveSnapshotPreview(BaseModel):
    special_leave_days: float
    daily_wage: float
    payout_amount: float


class SalaryRecordTarget(BaseModel):
    year: int
    month: int
    exists: bool
    will_be_marked_stale: bool


class AppraisalInFlightCycle(BaseModel):
    cycle_id: int
    cycle_name: str
    current_score: Optional[float] = None


class OffboardingPreview(BaseModel):
    user_account_will_be_revoked: bool
    leave_snapshot: LeaveSnapshotPreview
    salary_record_target: SalaryRecordTarget
    appraisal_in_flight_cycles: list[AppraisalInFlightCycle]
    certificate_pdf_ready_to_generate: bool


class OffboardingPreviewResponse(BaseModel):
    employee_id: int
    employee_name: str
    resign_date: date
    preview: OffboardingPreview
    warnings: list[str] = Field(default_factory=list)


class StepResultModel(BaseModel):
    step: str
    status: Literal["completed", "skipped", "failed"]
    completed_at: Optional[datetime] = None
    payload: Optional[dict] = None
    error: Optional[str] = None


class OffboardingProcessResponse(BaseModel):
    employee_id: int
    resign_date: date
    is_active: bool
    user_account_revoked: bool
    steps: list[StepResultModel]
    certificate_download_url: Optional[str] = None  # Phase 1 一律 None


class OffboardingDetailResponse(BaseModel):
    employee_id: int
    employee_name: str
    resign_date: date
    resign_reason: Optional[str]
    opened_at: datetime
    opened_by_user_id: int
    appraisal_marked_at: Optional[datetime]
    leave_snapshot_at: Optional[datetime]
    user_revoked_at: Optional[datetime]
    certificate_generated_at: Optional[datetime]
    leave_balance_snapshot: Optional[dict]
    certificate_pdf_path: Optional[str]
    nhi_unenroll_submitted_at: Optional[datetime]
    magic_link_active: bool  # 派生：token_hash & not revoked & not expired & count<3
    closed_at: Optional[datetime]


class NhiUnenrollRequest(BaseModel):
    submitted: bool
