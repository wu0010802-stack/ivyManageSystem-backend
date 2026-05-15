"""年終特別獎金（統一表）router：8 種 bonus_type 共用一支 CRUD。"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.database import get_session_dep
from models.year_end import (
    SpecialBonusType,
    YearEndCycle,
    YearEndCycleStatus,
    YearEndSpecialBonusItem,
)
from schemas.year_end import SpecialBonusOut, SpecialBonusUpsert
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/cycles/{cycle_id}/special_bonuses",
    response_model=list[SpecialBonusOut],
)
def list_special_bonuses(
    cycle_id: int,
    bonus_type: Optional[SpecialBonusType] = None,
    employee_id: Optional[int] = None,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_READ)),
):
    stmt = select(YearEndSpecialBonusItem).where(
        YearEndSpecialBonusItem.cycle_id == cycle_id
    )
    if bonus_type is not None:
        stmt = stmt.where(YearEndSpecialBonusItem.bonus_type == bonus_type)
    if employee_id is not None:
        stmt = stmt.where(YearEndSpecialBonusItem.employee_id == employee_id)
    return db.execute(stmt).scalars().all()


@router.post(
    "/cycles/{cycle_id}/special_bonuses",
    response_model=SpecialBonusOut,
    status_code=201,
)
def upsert_special_bonus(
    cycle_id: int,
    payload: SpecialBonusUpsert,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_WRITE)),
):
    cycle = db.get(YearEndCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    if cycle.status == YearEndCycleStatus.PAID:
        raise HTTPException(400, "cycle_paid")

    existing = db.execute(
        select(YearEndSpecialBonusItem).where(
            YearEndSpecialBonusItem.cycle_id == cycle_id,
            YearEndSpecialBonusItem.employee_id == payload.employee_id,
            YearEndSpecialBonusItem.bonus_type == payload.bonus_type,
            YearEndSpecialBonusItem.period_label == payload.period_label,
        )
    ).scalar_one_or_none()

    if existing is None:
        existing = YearEndSpecialBonusItem(
            cycle_id=cycle_id,
            employee_id=payload.employee_id,
            bonus_type=payload.bonus_type,
            period_label=payload.period_label,
            amount=payload.amount,
            calc_meta=payload.calc_meta,
        )
        db.add(existing)
    else:
        existing.amount = payload.amount
        existing.calc_meta = payload.calc_meta

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "special_bonus_conflict")
    db.refresh(existing)
    request.state.audit_entity_id = existing.id
    return existing


@router.delete("/special_bonuses/{item_id}", status_code=204)
def delete_special_bonus(
    item_id: int,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_WRITE)),
):
    item = db.get(YearEndSpecialBonusItem, item_id)
    if not item:
        raise HTTPException(404, "special_bonus_not_found")
    cycle = db.get(YearEndCycle, item.cycle_id)
    if cycle.status == YearEndCycleStatus.PAID:
        raise HTTPException(400, "cycle_paid")
    db.delete(item)
    db.commit()
    request.state.audit_entity_id = item_id
    return
