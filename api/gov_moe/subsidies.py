"""特教加給 / 助理鐘點費 申領 endpoints (Phase 4B)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.gov_moe.disability_documents import get_db
from models.gov_moe import SpecialEducationSubsidy
from utils.auth import require_staff_permission, require_admin
from utils.permissions import Permission

router = APIRouter(prefix="/subsidies")


class SubsidyBase(BaseModel):
    subsidy_type: str = Field(..., pattern="^(teacher_extra|assistant_hourly)$")
    employee_id: int
    related_student_ids: Optional[List[int]] = None
    period_start: date
    period_end: date
    hours_or_rate: Optional[Decimal] = None
    amount_requested: Decimal = Field(0, ge=0)
    notes: Optional[str] = None


class SubsidyCreate(SubsidyBase):
    pass


class SubsidyUpdate(BaseModel):
    related_student_ids: Optional[List[int]] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    hours_or_rate: Optional[Decimal] = None
    amount_requested: Optional[Decimal] = Field(None, ge=0)
    notes: Optional[str] = None


class ApproveRequest(BaseModel):
    amount_approved: Decimal = Field(..., ge=0)
    notes: Optional[str] = None


class MarkPaidRequest(BaseModel):
    paid_at: datetime
    approval_doc_path: Optional[str] = None


class SubsidyOut(BaseModel):
    id: int
    subsidy_type: str
    employee_id: int
    related_student_ids: Optional[List[int]] = None
    period_start: date
    period_end: date
    hours_or_rate: Optional[Decimal] = None
    amount_requested: Decimal
    amount_approved: Optional[Decimal] = None
    status: str
    applied_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    approval_doc_path: Optional[str] = None
    notes: Optional[str] = None

    class Config:
        from_attributes = True


@router.get("", response_model=List[SubsidyOut])
def list_subsidies(
    employee_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    since: Optional[date] = None,
    until: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_staff_permission(Permission.GOV_REPORTS_VIEW)),
):
    q = db.query(SpecialEducationSubsidy)
    if employee_id:
        q = q.filter(SpecialEducationSubsidy.employee_id == employee_id)
    if status_filter:
        q = q.filter(SpecialEducationSubsidy.status == status_filter)
    if since:
        q = q.filter(SpecialEducationSubsidy.period_end >= since)
    if until:
        q = q.filter(SpecialEducationSubsidy.period_start <= until)
    return q.order_by(SpecialEducationSubsidy.id.desc()).limit(500).all()


@router.post("", response_model=SubsidyOut, status_code=status.HTTP_201_CREATED)
def create_subsidy(
    payload: SubsidyCreate,
    db: Session = Depends(get_db),
    _: dict = Depends(require_admin),
):
    if payload.period_end < payload.period_start:
        raise HTTPException(422, "period_end < period_start")
    row = SpecialEducationSubsidy(**payload.model_dump(), status="draft")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/{sub_id}", response_model=SubsidyOut)
def update_subsidy(
    sub_id: int,
    payload: SubsidyUpdate,
    db: Session = Depends(get_db),
    _: dict = Depends(require_admin),
):
    row = db.query(SpecialEducationSubsidy).get(sub_id)
    if not row:
        raise HTTPException(404, "Not found")
    if row.status not in ("draft", "submitted"):
        raise HTTPException(409, f"Cannot edit subsidy in status {row.status}")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


@router.put("/{sub_id}/submit", response_model=SubsidyOut)
def submit_subsidy(
    sub_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_admin),
):
    row = db.query(SpecialEducationSubsidy).get(sub_id)
    if not row:
        raise HTTPException(404, "Not found")
    if row.status != "draft":
        raise HTTPException(409, "Only draft → submitted allowed")
    row.status = "submitted"
    row.applied_at = datetime.now()
    db.commit()
    db.refresh(row)
    return row


@router.put("/{sub_id}/approve", response_model=SubsidyOut)
def approve_subsidy(
    sub_id: int,
    payload: ApproveRequest,
    db: Session = Depends(get_db),
    _: dict = Depends(require_admin),
):
    row = db.query(SpecialEducationSubsidy).get(sub_id)
    if not row:
        raise HTTPException(404, "Not found")
    if row.status != "submitted":
        raise HTTPException(409, "Only submitted → approved allowed")
    row.amount_approved = payload.amount_approved
    if payload.notes:
        row.notes = (row.notes or "") + f"\n[approve] {payload.notes}"
    row.status = "approved"
    row.approved_at = datetime.now()
    db.commit()
    db.refresh(row)
    return row


@router.put("/{sub_id}/mark_paid", response_model=SubsidyOut)
def mark_paid(
    sub_id: int,
    payload: MarkPaidRequest,
    db: Session = Depends(get_db),
    _: dict = Depends(require_admin),
):
    row = db.query(SpecialEducationSubsidy).get(sub_id)
    if not row:
        raise HTTPException(404, "Not found")
    if row.status != "approved":
        raise HTTPException(409, "Only approved → paid allowed")
    row.status = "paid"
    row.paid_at = payload.paid_at
    if payload.approval_doc_path:
        row.approval_doc_path = payload.approval_doc_path
    db.commit()
    db.refresh(row)
    return row


@router.put("/{sub_id}/reject", response_model=SubsidyOut)
def reject_subsidy(
    sub_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_admin),
):
    row = db.query(SpecialEducationSubsidy).get(sub_id)
    if not row:
        raise HTTPException(404, "Not found")
    if row.status not in ("submitted", "approved"):
        raise HTTPException(409, "Only submitted/approved → rejected allowed")
    row.status = "rejected"
    db.commit()
    db.refresh(row)
    return row
