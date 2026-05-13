"""Disability documents CRUD (Phase 1)."""

from datetime import date
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from models.database import get_session
from models.gov_moe import StudentDisabilityDocument
from utils.permissions import Permission
from utils.auth import require_staff_permission
from utils.portfolio_access import (
    assert_student_access,
    student_ids_in_scope,
)

router = APIRouter(prefix="/disability-documents")


def get_db():
    session = get_session()
    try:
        yield session
    finally:
        session.close()


def _validate_file_path(value: str) -> str:
    """拒絕 path traversal / 含 null byte 的 file_path。

    file_path 由 client 直接寫入 DB，若未來下載端點 resolve 此欄位即可造成任意檔讀；
    在 ingress 階段就把明顯的攻擊形態擋掉是最小代價的 defense-in-depth。
    現行 baseline 將 `/uploads/...` 視為相對於上傳根目錄，故不擋絕對路徑 prefix；
    但 `..` 與 null byte 是 traversal 必經之路，必須拒絕。
    """
    if value is None:
        return value
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("file_path 不可為空")
    if ".." in cleaned or "\0" in cleaned or "\\" in cleaned:
        raise ValueError("file_path 含非法字元（不可含 .. / null byte / 反斜線）")
    return cleaned


class DisabilityDocCreate(BaseModel):
    student_id: int
    doc_type: str = Field(..., max_length=20)
    file_path: str = Field(..., max_length=500)
    issued_date: Optional[date] = None
    expiry_date: Optional[date] = None
    notes: Optional[str] = None

    @field_validator("file_path")
    @classmethod
    def _check_file_path(cls, v: str) -> str:
        return _validate_file_path(v)


class DisabilityDocUpdate(BaseModel):
    doc_type: Optional[str] = Field(None, max_length=20)
    file_path: Optional[str] = Field(None, max_length=500)
    issued_date: Optional[date] = None
    expiry_date: Optional[date] = None
    notes: Optional[str] = None

    @field_validator("file_path")
    @classmethod
    def _check_file_path(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_file_path(v)


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
        # 指定 student_id 時走 access check（與其他 student-bound 端點對齊，
        # 同時遮蔽「存在但無權」與「不存在」差異）
        assert_student_access(db, current_user, student_id)
        q = q.filter(StudentDisabilityDocument.student_id == student_id)
    else:
        # 未指定 student 時依 caller scope 過濾；admin/hr/supervisor 拿 None
        # 表全放行，teacher 縮到自己班級學生。
        allowed = student_ids_in_scope(db, current_user)
        if allowed is None:
            pass  # 無限制
        elif not allowed:
            return []
        else:
            q = q.filter(StudentDisabilityDocument.student_id.in_(allowed))
    return q.order_by(StudentDisabilityDocument.created_at.desc()).all()


@router.post("", response_model=DisabilityDocOut, status_code=status.HTTP_201_CREATED)
def create_disability_doc(
    payload: DisabilityDocCreate,
    db: Session = Depends(get_db),
    # 寫入端點用 GOV_REPORTS_EXPORT 而非 VIEW，避免 read 權限者可竄改／刪除
    # 鑑定文件（讀寫權責分離）。
    current_user: dict = Depends(
        require_staff_permission(Permission.GOV_REPORTS_EXPORT)
    ),
):
    # 防止跨班學生建檔（defense-in-depth：即使預設只發 EXPORT 給 admin/hr/
    # supervisor，仍對 student_id 走 access check）
    assert_student_access(db, current_user, payload.student_id)

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
    current_user: dict = Depends(
        require_staff_permission(Permission.GOV_REPORTS_EXPORT)
    ),
):
    doc = db.query(StudentDisabilityDocument).filter_by(id=doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="鑑定文件不存在")
    assert_student_access(db, current_user, doc.student_id)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(doc, k, v)
    db.commit()
    db.refresh(doc)
    return doc


@router.delete("/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_disability_doc(
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_staff_permission(Permission.GOV_REPORTS_EXPORT)
    ),
):
    doc = db.query(StudentDisabilityDocument).filter_by(id=doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="鑑定文件不存在")
    assert_student_access(db, current_user, doc.student_id)
    db.delete(doc)
    db.commit()
    return None
