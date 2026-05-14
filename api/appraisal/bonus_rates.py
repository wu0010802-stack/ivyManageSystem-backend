"""考核獎金率 versioned setting router。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.appraisal import AppraisalBonusRate
from models.database import get_session_dep
from schemas.appraisal import BonusRateCreate, BonusRateOut
from utils.auth import require_staff_permission
from utils.permissions import Permission

router = APIRouter()


@router.get("/bonus_rates", response_model=list[BonusRateOut])
def list_rates(
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    return (
        db.execute(
            select(AppraisalBonusRate).order_by(
                AppraisalBonusRate.effective_from.desc(),
                AppraisalBonusRate.role_group,
                AppraisalBonusRate.grade,
            )
        )
        .scalars()
        .all()
    )


@router.post("/bonus_rates", response_model=BonusRateOut, status_code=201)
def create_rate(
    payload: BonusRateCreate,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    r = AppraisalBonusRate(
        effective_from=payload.effective_from,
        role_group=payload.role_group,
        grade=payload.grade,
        base_amount=payload.base_amount,
        created_by=current_user["user_id"],
    )
    db.add(r)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            409,
            "bonus_rate_conflict:同日期+role_group+grade 已存在",
        )
    db.refresh(r)
    request.state.audit_entity_id = r.id
    return r
