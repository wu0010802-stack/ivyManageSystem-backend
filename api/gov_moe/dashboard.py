"""Dashboard widget endpoints for MOE module (Phase 1)."""

from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.classroom import Student
from utils.permissions import Permission
from utils.auth import require_staff_permission

# get_db is exported from disability_documents — reuse to share the same
# monkey-patched _SessionFactory in tests.
from api.gov_moe.disability_documents import get_db

router = APIRouter(prefix="/dashboard")


class ExpiringStudentRow(BaseModel):
    id: int
    student_id: str
    name: str
    classroom_id: Optional[int]
    disability_cert_no: Optional[str]
    disability_cert_expiry: date
    days_remaining: int


class DisabilityExpiryResponse(BaseModel):
    total: int
    days_window: int
    students: List[ExpiringStudentRow]


@router.get("/disability-expiry", response_model=DisabilityExpiryResponse)
def disability_expiry_widget(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_staff_permission(Permission.GOV_REPORTS_VIEW)),
):
    today = date.today()
    end = today + timedelta(days=days)
    rows = (
        db.query(Student)
        .filter(Student.is_active == True)  # noqa: E712
        .filter(Student.disability_cert_expiry != None)  # noqa: E711
        .filter(Student.disability_cert_expiry >= today)
        .filter(Student.disability_cert_expiry <= end)
        .order_by(Student.disability_cert_expiry.asc())
        .all()
    )
    students = [
        ExpiringStudentRow(
            id=s.id,
            student_id=s.student_id,
            name=s.name,
            classroom_id=s.classroom_id,
            disability_cert_no=s.disability_cert_no,
            disability_cert_expiry=s.disability_cert_expiry,
            days_remaining=(s.disability_cert_expiry - today).days,
        )
        for s in rows
    ]
    return DisabilityExpiryResponse(
        total=len(students), days_window=days, students=students
    )
