"""半年考核項目 router（取代舊版 events router）。

每 participant 對 16 項 score_item 採 upsert 寫入；UNIQUE(participant_id, item_code) 保證冪等。
事件變更會將 summary 標 stale 並重算。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalScoreItem,
    AppraisalScoreItemCatalog,
    CycleStatus,
)
from models.database import get_session_dep
from schemas.appraisal import ScoreItemOut, ScoreItemPatch, ScoreItemUpsert
from services.appraisal.summary_ops import mark_summary_stale
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)
router = APIRouter()


def _resolve_user_employee_id(current_user: dict) -> Optional[int]:
    return current_user.get("employee_id")


def _guard_writable(
    db: Session, participant: AppraisalParticipant, current_user: dict
) -> AppraisalCycle:
    cycle = db.get(AppraisalCycle, participant.cycle_id)
    if cycle.status == CycleStatus.CLOSED:
        raise HTTPException(400, "cycle_closed")
    if cycle.status == CycleStatus.LOCKED:
        raise HTTPException(400, "cycle_locked:LOCKED 期間禁止編輯")
    actor_emp_id = _resolve_user_employee_id(current_user)
    if actor_emp_id is not None and participant.employee_id == actor_emp_id:
        raise HTTPException(403, "self_item_forbidden:不能登錄自己的考核項目")
    return cycle


@router.get("/score_items", response_model=list[ScoreItemOut])
def list_score_items(
    cycle_id: Optional[int] = None,
    participant_id: Optional[int] = None,
    item_code: Optional[str] = None,
    limit: int = Query(default=500, le=2000),
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    stmt = select(AppraisalScoreItem).order_by(
        AppraisalScoreItem.participant_id, AppraisalScoreItem.item_code
    )
    if participant_id is not None:
        stmt = stmt.where(AppraisalScoreItem.participant_id == participant_id)
    if cycle_id is not None:
        stmt = (
            stmt.join(
                AppraisalParticipant,
                AppraisalParticipant.id == AppraisalScoreItem.participant_id,
            )
            .where(AppraisalParticipant.cycle_id == cycle_id)
        )
    if item_code is not None:
        stmt = stmt.where(AppraisalScoreItem.item_code == item_code)
    stmt = stmt.limit(limit)
    return db.execute(stmt).scalars().all()


@router.post("/score_items", response_model=ScoreItemOut, status_code=201)
def upsert_score_item(
    payload: ScoreItemUpsert,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_EVENT_WRITE)
    ),
):
    """新增或更新項目（同 participant + item_code 走 upsert）。"""
    participant = db.get(AppraisalParticipant, payload.participant_id)
    if not participant:
        raise HTTPException(404, "participant_not_found")
    _guard_writable(db, participant, current_user)

    catalog = db.execute(
        select(AppraisalScoreItemCatalog).where(
            AppraisalScoreItemCatalog.code == payload.item_code,
            AppraisalScoreItemCatalog.is_active == True,  # noqa: E712
        )
    ).scalar_one_or_none()
    if catalog is None:
        raise HTTPException(404, "score_item_catalog_not_found")

    existing = db.execute(
        select(AppraisalScoreItem).where(
            AppraisalScoreItem.participant_id == payload.participant_id,
            AppraisalScoreItem.item_code == payload.item_code,
        )
    ).scalar_one_or_none()

    if existing is None:
        item = AppraisalScoreItem(
            participant_id=payload.participant_id,
            item_code=payload.item_code,
            score_delta=payload.score_delta,
            raw_value=payload.raw_value,
            note=payload.note,
            created_by=current_user.get("user_id"),
        )
        db.add(item)
    else:
        existing.score_delta = payload.score_delta
        existing.raw_value = payload.raw_value
        existing.note = payload.note
        item = existing

    db.flush()

    try:
        mark_summary_stale(db, participant.id)
    except PermissionError as e:
        db.rollback()
        raise HTTPException(409, str(e))

    db.commit()
    db.refresh(item)
    request.state.audit_entity_id = item.id
    return item


@router.patch("/score_items/{item_id}", response_model=ScoreItemOut)
def patch_score_item(
    item_id: int,
    payload: ScoreItemPatch,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_EVENT_WRITE)
    ),
):
    item = db.get(AppraisalScoreItem, item_id)
    if not item:
        raise HTTPException(404, "score_item_not_found")
    participant = db.get(AppraisalParticipant, item.participant_id)
    _guard_writable(db, participant, current_user)

    if payload.score_delta is not None:
        item.score_delta = payload.score_delta
    if payload.raw_value is not None:
        item.raw_value = payload.raw_value
    if payload.note is not None:
        item.note = payload.note
    db.flush()

    try:
        mark_summary_stale(db, participant.id)
    except PermissionError as e:
        db.rollback()
        raise HTTPException(409, str(e))

    db.commit()
    db.refresh(item)
    request.state.audit_entity_id = item.id
    return item


@router.delete("/score_items/{item_id}", status_code=204)
def delete_score_item(
    item_id: int,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_EVENT_WRITE)
    ),
):
    item = db.get(AppraisalScoreItem, item_id)
    if not item:
        raise HTTPException(404, "score_item_not_found")
    participant = db.get(AppraisalParticipant, item.participant_id)
    _guard_writable(db, participant, current_user)
    db.delete(item)
    db.flush()

    try:
        mark_summary_stale(db, participant.id)
    except PermissionError as e:
        db.rollback()
        raise HTTPException(409, str(e))

    db.commit()
    request.state.audit_entity_id = item_id
    return
