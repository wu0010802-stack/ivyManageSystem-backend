"""Disability documents CRUD (Phase 1)."""

from datetime import date
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from models.database import get_session
from models.gov_moe import StudentDisabilityDocument
from utils.permissions import Permission
from utils.auth import require_staff_permission

router = APIRouter(prefix="/disability-documents")


def get_db():
    session = get_session()
    try:
        yield session
    finally:
        session.close()


class DisabilityDocCreate(BaseModel):
    student_id: int
    doc_type: str = Field(..., max_length=20)
    file_path: str = Field(..., max_length=500)
    issued_date: Optional[date] = None
    expiry_date: Optional[date] = None
    notes: Optional[str] = None


class DisabilityDocUpdate(BaseModel):
    doc_type: Optional[str] = Field(None, max_length=20)
    file_path: Optional[str] = Field(None, max_length=500)
    issued_date: Optional[date] = None
    expiry_date: Optional[date] = None
    notes: Optional[str] = None


class DisabilityDocOut(BaseModel):
    id: int
    student_id: int
    doc_type: str
    file_path: str
    issued_date: Optional[date]
    expiry_date: Optional[date]
    notes: Optional[str]

    class Config:
        from_attributes = True


@router.get("", response_model=List[DisabilityDocOut])
def list_disability_docs(
    student_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_staff_permission(Permission.GOV_REPORTS_VIEW)),
):
    q = db.query(StudentDisabilityDocument)
    if student_id is not None:
        q = q.filter(StudentDisabilityDocument.student_id == student_id)
    return q.order_by(StudentDisabilityDocument.created_at.desc()).all()


@router.post("", response_model=DisabilityDocOut, status_code=status.HTTP_201_CREATED)
def create_disability_doc(
    payload: DisabilityDocCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_staff_permission(Permission.GOV_REPORTS_VIEW)),
):
    doc = StudentDisabilityDocument(**payload.model_dump())
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@router.put("/{doc_id}", response_model=DisabilityDocOut)
def update_disability_doc(
    doc_id: int,
    payload: DisabilityDocUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_staff_permission(Permission.GOV_REPORTS_VIEW)),
):
    doc = db.query(StudentDisabilityDocument).filter_by(id=doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="鑑定文件不存在")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(doc, k, v)
    db.commit()
    db.refresh(doc)
    return doc


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_disability_doc(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_staff_permission(Permission.GOV_REPORTS_VIEW)),
):
    doc = db.query(StudentDisabilityDocument).filter_by(id=doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="鑑定文件不存在")
    db.delete(doc)
    db.commit()
    return None
