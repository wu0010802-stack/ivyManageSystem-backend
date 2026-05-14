"""考核懲處目錄 router。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import AppraisalPenaltyCatalogItem, CatalogCategory
from models.database import get_session_dep
from schemas.appraisal import CatalogOut, CatalogPatch
from utils.auth import require_staff_permission
from utils.permissions import Permission

router = APIRouter()


@router.get("/penalty_catalog", response_model=list[CatalogOut])
def list_catalog(
    active_only: bool = True,
    category: Optional[CatalogCategory] = None,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    stmt = select(AppraisalPenaltyCatalogItem).order_by(
        AppraisalPenaltyCatalogItem.display_order,
        AppraisalPenaltyCatalogItem.id,
    )
    if active_only:
        stmt = stmt.where(AppraisalPenaltyCatalogItem.is_active.is_(True))
    if category is not None:
        stmt = stmt.where(AppraisalPenaltyCatalogItem.category == category)
    return db.execute(stmt).scalars().all()


@router.patch("/penalty_catalog/{item_id}", response_model=CatalogOut)
def patch_catalog(
    item_id: int,
    payload: CatalogPatch,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    item = db.get(AppraisalPenaltyCatalogItem, item_id)
    if not item:
        raise HTTPException(404, "catalog_item_not_found")
    if payload.default_score_delta is not None:
        item.default_score_delta = payload.default_score_delta
    if payload.severity_max is not None:
        item.severity_max = payload.severity_max
    if payload.is_active is not None:
        item.is_active = payload.is_active
    if payload.display_order is not None:
        item.display_order = payload.display_order
    db.commit()
    db.refresh(item)
    request.state.audit_entity_id = item.id
    return item
