"""IEP individualized education program endpoints (Phase 4A)."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.gov_moe.disability_documents import get_db
from models.classroom import Student
from models.gov_moe import StudentIEPRecord
from utils.auth import require_permission, get_current_user
from utils.permissions import Permission
from utils.portfolio_access import (
    assert_student_access,
    student_ids_in_scope,
)

router = APIRouter(prefix="/iep")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class IepBase(BaseModel):
    student_id: int
    school_year: int = Field(..., ge=2020, le=2100)
    semester: int = Field(..., ge=1, le=2)
    current_status: Optional[str] = None
    long_term_goals: Optional[str] = None
    short_term_goals: Optional[List[dict]] = None
    mid_term_evaluation: Optional[str] = None
    final_evaluation: Optional[str] = None
    iep_team_members: Optional[List[dict]] = None
    meeting_dates: Optional[dict] = None


class IepCreate(IepBase):
    pass


class IepUpdate(BaseModel):
    current_status: Optional[str] = None
    long_term_goals: Optional[str] = None
    short_term_goals: Optional[List[dict]] = None
    mid_term_evaluation: Optional[str] = None
    final_evaluation: Optional[str] = None
    iep_team_members: Optional[List[dict]] = None
    meeting_dates: Optional[dict] = None


class IepCloneRequest(BaseModel):
    target_school_year: int = Field(..., ge=2020, le=2100)
    target_semester: int = Field(..., ge=1, le=2)


class IepOut(IepBase):
    id: int
    status: str
    created_by_employee_id: Optional[int] = None
    approved_by_employee_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Access-scoping helper
# ---------------------------------------------------------------------------


def _student_ids_in_scope(db: Session, current_user: dict):
    """回傳 caller 可存取的 student_id 集合；None 表示全部放行（admin / :all scope）。

    delegate 至 utils/portfolio_access.student_ids_in_scope —
    含 PermissionGrant :all / :own_class scope 支援 + lifecycle 終態學生過濾
    （audit 2026-05-07 P0 #5：已退學/畢業/轉出學生對 teacher 立即不可見）。

    回傳值：None 表全放行；空 list 表無存取權；否則為可存取 student_id 清單。
    既有 _scoped_query 的 ``if allowed is None`` / ``if not allowed`` / ``in allowed``
    判斷對 list 仍正確（``in`` 對 list 線性搜尋；scope 端點預期清單大小 < 數百）。
    """
    return student_ids_in_scope(
        db, current_user, code=Permission.STUDENTS_SPECIAL_NEEDS_WRITE.value
    )


def _assert_student_in_scope(db: Session, current_user: dict, student_id: int) -> None:
    """寫入端點用：student_id 來自 body 時必須驗證該學生在 caller scope 內。

    沒這層守衛則持 STUDENTS_SPECIAL_NEEDS_WRITE 的班導可為跨班學生建檔，
    既污染他班 IEP、又會佔用 (student, year, semester) 唯一鍵。

    delegate 至 utils/portfolio_access.assert_student_access —
    含 PermissionGrant :all / :own_class scope + lifecycle 終態學生過濾
    （audit 2026-05-07 P0 #5）+ 404/403 collapse 不揭露 student 是否存在。
    """
    assert_student_access(
        db,
        current_user,
        student_id,
        code=Permission.STUDENTS_SPECIAL_NEEDS_WRITE.value,
    )


def _is_supervisor_or_above(db: Session, current_user: dict) -> bool:
    """admin / 園長 / 主任 可批核或結案 IEP。

    JWT payload 不含 supervisor_role（見 api/auth.py：create_access_token 只放
    user_id/employee_id/role/name/permissions/token_version），故對非 admin
    用戶必須 DB lookup Employee 表 — 否則園長/主任 100% 卡 403。
    """
    if current_user.get("role") == "admin":
        return True
    employee_id = current_user.get("employee_id")
    if not employee_id:
        return False
    from models.employee import Employee

    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    return bool(emp and emp.supervisor_role in ("園長", "主任"))


def _scoped_query(db: Session, current_user: dict):
    """班導/副班導 只看自己班級的 IEP；主任以上看全部。

    JWT payload 不含 supervisor_role / classroom_id，故對非 admin 用戶
    執行 DB lookup（Employee 表），再依 supervisor_role + classroom_id 套用範圍。
    """
    q = db.query(StudentIEPRecord).filter(
        StudentIEPRecord.deleted_at == None  # noqa: E711
    )
    allowed = _student_ids_in_scope(db, current_user)
    if allowed is None:
        return q
    if not allowed:
        return q.filter(False)
    return q.filter(StudentIEPRecord.student_id.in_(allowed))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=List[IepOut])
def list_iep(
    student_id: Optional[int] = None,
    school_year: Optional[int] = None,
    semester: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_permission(Permission.STUDENTS_SPECIAL_NEEDS_WRITE)
    ),
):
    q = _scoped_query(db, current_user)
    if student_id:
        q = q.filter(StudentIEPRecord.student_id == student_id)
    if school_year:
        q = q.filter(StudentIEPRecord.school_year == school_year)
    if semester:
        q = q.filter(StudentIEPRecord.semester == semester)
    return q.order_by(StudentIEPRecord.id.desc()).limit(500).all()


@router.post("", response_model=IepOut, status_code=status.HTTP_201_CREATED)
def create_iep(
    payload: IepCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_permission(Permission.STUDENTS_SPECIAL_NEEDS_WRITE)
    ),
):
    # student_id 來自 body：必須驗證該學生在 caller 的班級 scope 內，
    # 否則持 STUDENTS_SPECIAL_NEEDS_WRITE 的班導可為跨班學生建檔並佔唯一鍵。
    _assert_student_in_scope(db, current_user, payload.student_id)

    row = StudentIEPRecord(
        **payload.model_dump(exclude_none=False),
        status="draft",
        created_by_employee_id=current_user.get("employee_id"),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="IEP already exists for this student/year/semester",
        )
    db.refresh(row)
    return row


@router.put("/{iep_id}", response_model=IepOut)
def update_iep(
    iep_id: int,
    payload: IepUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_permission(Permission.STUDENTS_SPECIAL_NEEDS_WRITE)
    ),
):
    row = _scoped_query(db, current_user).filter(StudentIEPRecord.id == iep_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found or no access")
    if row.status not in ("draft", "pending_review"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot edit IEP in status {row.status}",
        )
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


@router.put("/{iep_id}/submit", response_model=IepOut)
def submit_iep(
    iep_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_permission(Permission.STUDENTS_SPECIAL_NEEDS_WRITE)
    ),
):
    row = _scoped_query(db, current_user).filter(StudentIEPRecord.id == iep_id).first()
    if not row:
        raise HTTPException(status_code=404)
    if row.status != "draft":
        raise HTTPException(
            status_code=409,
            detail="Only draft → pending_review transition allowed",
        )
    row.status = "pending_review"
    db.commit()
    db.refresh(row)
    return row


@router.put("/{iep_id}/approve", response_model=IepOut)
def approve_iep(
    iep_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if not _is_supervisor_or_above(db, current_user):
        raise HTTPException(
            status_code=403,
            detail="Only 主任 or above can approve IEP",
        )
    row = (
        db.query(StudentIEPRecord)
        .filter(
            StudentIEPRecord.id == iep_id,
            StudentIEPRecord.deleted_at == None,  # noqa: E711
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404)
    if row.status != "pending_review":
        raise HTTPException(
            status_code=409,
            detail="Only pending_review → approved transition allowed",
        )
    row.status = "approved"
    row.approved_by_employee_id = current_user.get("employee_id")
    db.commit()
    db.refresh(row)
    return row


@router.put("/{iep_id}/close", response_model=IepOut)
def close_iep(
    iep_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if not _is_supervisor_or_above(db, current_user):
        raise HTTPException(status_code=403)
    row = (
        db.query(StudentIEPRecord)
        .filter(
            StudentIEPRecord.id == iep_id,
            StudentIEPRecord.deleted_at == None,  # noqa: E711
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404)
    if row.status != "approved":
        raise HTTPException(
            status_code=409,
            detail="Only approved → closed transition allowed",
        )
    row.status = "closed"
    db.commit()
    db.refresh(row)
    return row


@router.get("/{iep_id}/export")
def export_iep_pdf(
    iep_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_permission(Permission.STUDENTS_SPECIAL_NEEDS_WRITE)
    ),
):
    import urllib.parse

    from fastapi.responses import Response

    from services.iep_pdf import generate_iep_pdf

    row = _scoped_query(db, current_user).filter(StudentIEPRecord.id == iep_id).first()
    if not row:
        raise HTTPException(404)
    student = db.query(Student).filter(Student.id == row.student_id).first()
    pdf = generate_iep_pdf(
        student_name=student.name if student else "（未知）",
        school_year=row.school_year,
        semester=row.semester,
        current_status=row.current_status or "",
        long_term_goals=row.long_term_goals or "",
        short_term_goals=row.short_term_goals or [],
        mid_term_evaluation=row.mid_term_evaluation or "",
        final_evaluation=row.final_evaluation or "",
        iep_team_members=row.iep_team_members or [],
        meeting_dates=row.meeting_dates or {},
    )
    # ASCII-safe filename + RFC 5987 UTF-8 fallback (Chinese filename needs encoding)
    student_label = student.name if student else f"iep_{iep_id}"
    raw_name = f"IEP_{student_label}_{row.school_year}-{row.semester}.pdf"
    encoded = urllib.parse.quote(raw_name)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=iep_{iep_id}.pdf; filename*=UTF-8''{encoded}"
        },
    )


@router.post(
    "/{iep_id}/clone",
    response_model=IepOut,
    status_code=status.HTTP_201_CREATED,
)
def clone_iep(
    iep_id: int,
    payload: IepCloneRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_permission(Permission.STUDENTS_SPECIAL_NEEDS_WRITE)
    ),
):
    src = _scoped_query(db, current_user).filter(StudentIEPRecord.id == iep_id).first()
    if not src:
        raise HTTPException(
            status_code=404,
            detail="Source IEP not found or no access",
        )

    new = StudentIEPRecord(
        student_id=src.student_id,
        school_year=payload.target_school_year,
        semester=payload.target_semester,
        status="draft",
        # Copy content fields
        current_status=src.current_status,
        long_term_goals=src.long_term_goals,
        short_term_goals=src.short_term_goals,
        iep_team_members=src.iep_team_members,
        # Clear evaluation / meeting fields per spec §7.1.3
        mid_term_evaluation=None,
        final_evaluation=None,
        meeting_dates=None,
        created_by_employee_id=current_user.get("employee_id"),
    )
    db.add(new)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Target school_year/semester already has IEP for this student",
        )
    db.refresh(new)
    return new
