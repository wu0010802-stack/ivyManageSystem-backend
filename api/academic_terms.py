"""api/academic_terms.py — /academic-terms CRUD。

學年學期設定，供招生漏斗 scheduler 判斷開學日推進 enrolled→active。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.academic_term import AcademicTerm
from models.base import get_session_dep
from schemas.academic_term import AcademicTermIn, AcademicTermOut
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/academic-terms", tags=["academic-terms"])


@router.get("", response_model=list[AcademicTermOut])
def list_terms(
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
) -> list[AcademicTerm]:
    """列出所有學年學期，依學年學期倒序排列。"""
    return (
        session.query(AcademicTerm)
        .order_by(AcademicTerm.school_year.desc(), AcademicTerm.semester.desc())
        .all()
    )


@router.get("/current", response_model=Optional[AcademicTermOut])
def current_term(
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
) -> Optional[AcademicTerm]:
    """回傳今日所在學期（查無回 null）。"""
    today = date.today()
    return (
        session.query(AcademicTerm)
        .filter(AcademicTerm.start_date <= today, AcademicTerm.end_date >= today)
        .first()
    )


@router.post("", response_model=AcademicTermOut)
def create_term(
    payload: AcademicTermIn,
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
) -> AcademicTerm:
    """新增學年學期設定。"""
    term = AcademicTerm(**payload.model_dump())
    session.add(term)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, detail="已存在 (school_year, semester) 的設定")
    session.refresh(term)
    logger.info("新增學年學期 %s-%s", term.school_year, term.semester)
    return term


@router.put("/{term_id}", response_model=AcademicTermOut)
def update_term(
    term_id: int,
    payload: AcademicTermIn,
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
) -> AcademicTerm:
    """更新學年學期設定。"""
    term = session.get(AcademicTerm, term_id)
    if term is None:
        raise HTTPException(404, detail="學年學期不存在")
    for k, v in payload.model_dump().items():
        setattr(term, k, v)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, detail="違反 unique 約束 (school_year, semester)")
    session.refresh(term)
    logger.info("更新學年學期 id=%s → %s-%s", term_id, term.school_year, term.semester)
    return term


@router.delete("/{term_id}")
def delete_term(
    term_id: int,
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
) -> dict:
    """刪除學年學期設定。"""
    term = session.get(AcademicTerm, term_id)
    if term is None:
        raise HTTPException(404, detail="學年學期不存在")
    session.delete(term)
    session.flush()
    logger.info("刪除學年學期 id=%s (%s-%s)", term_id, term.school_year, term.semester)
    return {"ok": True}
