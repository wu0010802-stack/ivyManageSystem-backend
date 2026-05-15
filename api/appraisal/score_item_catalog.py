"""半年考核項目目錄（catalog）router — 取代舊版 penalty_catalog router。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import AppraisalScoreItemCatalog
from models.database import get_session_dep
from schemas.appraisal import CatalogOut, CatalogPatch
from utils.auth import require_staff_permission
from utils.permissions import Permission

router = APIRouter()


@router.get("/score_item_catalog", response_model=list[CatalogOut])
def list_catalog(
    active_only: bool = True,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    stmt = select(AppraisalScoreItemCatalog).order_by(
        AppraisalScoreItemCatalog.display_order,
        AppraisalScoreItemCatalog.id,
    )
    if active_only:
        stmt = stmt.where(AppraisalScoreItemCatalog.is_active.is_(True))
    return db.execute(stmt).scalars().all()


@router.patch("/score_item_catalog/{item_id}", response_model=CatalogOut)
def patch_catalog(
    item_id: int,
    payload: CatalogPatch,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    item = db.get(AppraisalScoreItemCatalog, item_id)
    if not item:
        raise HTTPException(404, "catalog_item_not_found")
    if payload.label is not None:
        item.label = payload.label
    if payload.default_weight is not None:
        item.default_weight = payload.default_weight
    if payload.data_source is not None:
        item.data_source = payload.data_source
    if payload.is_active is not None:
        item.is_active = payload.is_active
    if payload.display_order is not None:
        item.display_order = payload.display_order
    db.commit()
    db.refresh(item)
    request.state.audit_entity_id = item.id
    return item
