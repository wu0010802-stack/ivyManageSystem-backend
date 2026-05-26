"""在學證明 generate + history endpoints (Phase 4C)."""

from __future__ import annotations

import base64
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.gov_moe.disability_documents import get_db  # reuse SessionFactory patch
from models.classroom import Classroom, Student
from models.gov_moe import EnrollmentCertificate
from services.enrollment_certificate_pdf import generate_enrollment_cert_pdf
from utils.auth import require_staff_permission
from utils.permissions import Permission

router = APIRouter(prefix="/certificates")


class GenerateCertRequest(BaseModel):
    issue_date: date
    purpose: str = Field(..., min_length=1, max_length=200)
    copies: int = Field(1, ge=1, le=20)


class CertificateOut(BaseModel):
    id: int
    student_id: int
    serial: str
    purpose: str
    copies: int
    issue_date: date
    pdf_base64: Optional[str] = None

    class Config:
        from_attributes = True


def _next_seq(db: Session, year: int) -> int:
    """取得今年下一個序號（簡單版：依 SQLite 與 PG 通用，
    PG 上若多進程同時開立需在 router 層加 advisory lock）。"""
    last = (
        db.query(func.max(EnrollmentCertificate.seq))
        .filter(EnrollmentCertificate.year == year)
        .scalar()
    )
    return (last or 0) + 1


def _resolve_classroom_name(db: Session, student: Student) -> str:
    cid = getattr(student, "classroom_id", None)
    if not cid:
        return "（未分班）"
    cls = db.query(Classroom).filter(Classroom.id == cid).first()
    return cls.name if cls else "（未分班）"


@router.post(
    "/{student_id}/generate",
    response_model=CertificateOut,
    status_code=status.HTTP_201_CREATED,
)
def generate_certificate(
    student_id: int,
    payload: GenerateCertRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_staff_permission(Permission.GOV_REPORTS_EXPORT)
    ),
):
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(404, "Student not found")

    year = payload.issue_date.year
    seq = _next_seq(db, year)
    cert = EnrollmentCertificate(
        student_id=student_id,
        year=year,
        seq=seq,
        purpose=payload.purpose,
        copies=payload.copies,
        issue_date=payload.issue_date,
        issued_by_user_id=current_user.get("user_id"),
    )
    db.add(cert)
    db.commit()
    db.refresh(cert)

    # Audit middleware reads request.state.audit_entity_id after response
    request.state.audit_entity_id = cert.id

    pdf_bytes = generate_enrollment_cert_pdf(
        student_name=student.name,
        student_no=student.student_id or "",
        id_number=getattr(student, "id_number", None),
        admit_date=getattr(student, "enrollment_date", None)
        or date.today(),  # noqa: DTZ011
        classroom_name=_resolve_classroom_name(db, student),
        purpose=cert.purpose,
        issue_date=cert.issue_date,
        serial=cert.serial,
        copies=cert.copies,
    )
    return CertificateOut(
        id=cert.id,
        student_id=cert.student_id,
        serial=cert.serial,
        purpose=cert.purpose,
        copies=cert.copies,
        issue_date=cert.issue_date,
        pdf_base64=base64.b64encode(pdf_bytes).decode("ascii"),
    )


@router.get("/history", response_model=List[CertificateOut])
def list_history(
    student_id: Optional[int] = None,
    since: Optional[date] = None,
    until: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_staff_permission(Permission.GOV_REPORTS_VIEW)),
):
    q = db.query(EnrollmentCertificate)
    if student_id:
        q = q.filter(EnrollmentCertificate.student_id == student_id)
    if since:
        q = q.filter(EnrollmentCertificate.issue_date >= since)
    if until:
        q = q.filter(EnrollmentCertificate.issue_date <= until)
    rows = q.order_by(EnrollmentCertificate.created_at.desc()).limit(200).all()
    return [
        CertificateOut(
            id=r.id,
            student_id=r.student_id,
            serial=r.serial,
            purpose=r.purpose,
            copies=r.copies,
            issue_date=r.issue_date,
        )
        for r in rows
    ]
