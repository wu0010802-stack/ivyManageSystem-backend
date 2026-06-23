"""api/data_quality.py — Ch2 data quality 後台管理。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.base import get_session_dep
from models.data_quality import DataQualityReport
from services.data_quality_scheduler import run_data_quality_once
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.taipei_time import now_taipei_naive

router = APIRouter(prefix="/api/data-quality", tags=["data-quality"])


class ReportOut(BaseModel):
    id: int
    rule_code: str
    severity: str
    entity_type: str
    entity_id: str
    summary: str
    status: str
    detected_at: datetime
    last_seen_at: datetime
    ack_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    resolution_note: Optional[str] = None

    class Config:
        from_attributes = True


class ListReportsOut(BaseModel):
    items: list[ReportOut]
    total: int
    page: int
    page_size: int


class AckBody(BaseModel):
    note: Optional[str] = None


class ResolveBody(BaseModel):
    note: str


class IgnoreBody(BaseModel):
    note: str


class RunNowOut(BaseModel):
    detected: int
    new_open: int
    ran_at: str


@router.get("/reports", response_model=ListReportsOut)
def list_reports(
    status: Optional[str] = Query(None),
    rule_code: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session_dep),
    _: object = Depends(require_staff_permission(Permission.DATA_QUALITY_READ)),
):
    q = session.query(DataQualityReport)
    if status:
        q = q.filter(DataQualityReport.status == status)
    if rule_code:
        q = q.filter(DataQualityReport.rule_code == rule_code)
    if severity:
        q = q.filter(DataQualityReport.severity == severity)
    q = q.order_by(DataQualityReport.detected_at.desc())

    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return ListReportsOut(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


def _get_report_or_404(session: Session, report_id: int) -> DataQualityReport:
    row = (
        session.query(DataQualityReport)
        .filter(DataQualityReport.id == report_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    return row


@router.post("/reports/{report_id}/ack", response_model=dict)
def ack_report(
    report_id: int,
    body: AckBody,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.DATA_QUALITY_WRITE)
    ),
):
    row = _get_report_or_404(session, report_id)
    if row.status != "open":
        raise HTTPException(
            status_code=400, detail=f"cannot ack from status={row.status}"
        )
    row.status = "ack"
    row.ack_at = now_taipei_naive()
    row.ack_by = current_user.get("user_id")
    if body.note:
        row.resolution_note = body.note
    session.commit()
    return {"ok": True, "id": row.id, "status": row.status}


@router.post("/reports/{report_id}/resolve", response_model=dict)
def resolve_report(
    report_id: int,
    body: ResolveBody,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.DATA_QUALITY_WRITE)
    ),
):
    row = _get_report_or_404(session, report_id)
    if row.status == "fixed":
        return {"ok": True, "id": row.id, "status": row.status}  # idempotent
    row.status = "fixed"
    row.resolved_at = now_taipei_naive()
    row.resolution_note = body.note
    if not row.ack_at:
        row.ack_at = row.resolved_at
        row.ack_by = current_user.get("user_id")
    session.commit()
    return {"ok": True, "id": row.id, "status": row.status}


@router.post("/reports/{report_id}/ignore", response_model=dict)
def ignore_report(
    report_id: int,
    body: IgnoreBody,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.DATA_QUALITY_WRITE)
    ),
):
    row = _get_report_or_404(session, report_id)
    row.status = "ignored"
    row.ack_at = now_taipei_naive()
    row.ack_by = current_user.get("user_id")
    row.resolution_note = body.note
    session.commit()
    return {"ok": True, "id": row.id, "status": row.status}


@router.post("/run-now", response_model=RunNowOut)
def run_now(
    _: object = Depends(require_staff_permission(Permission.DATA_QUALITY_WRITE)),
):
    return RunNowOut(**run_data_quality_once())
