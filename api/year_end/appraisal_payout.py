"""考核年終 payout API（HR 手動 trigger 後寫 special_bonus_items 兩筆 FIRST/SECOND）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.base import get_session_dep
from models.year_end import SpecialBonusItem, SpecialBonusType, YearEndCycle
from schemas.year_end import (
    PayoutGenerateRequest,
    PayoutGenerateResult,
    PayoutItem,
    PayoutPreviewRow,
)
from services.year_end.appraisal_sync import (
    civil_year_to_target_academic_year,
    generate_payouts,
    preview_payout,
    void_payouts,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/appraisal-payout", tags=["year_end:appraisal_payout"])


@router.get("/preview", response_model=list[PayoutPreviewRow])
def get_preview(
    year: int = Query(..., ge=2024, le=2099),
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_FINALIZE)
    ),
):
    try:
        rows = preview_payout(session, payout_year=year)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return [PayoutPreviewRow(**vars(r)) for r in rows]


@router.post("/generate", response_model=PayoutGenerateResult)
def post_generate(
    body: PayoutGenerateRequest,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_FINALIZE)
    ),
):
    result = generate_payouts(
        session,
        payout_year=body.year,
        included_inactive_employee_ids=set(body.included_inactive_employee_ids),
        generated_by=current_user.get("user_id", 0),
    )
    session.commit()
    return PayoutGenerateResult(**vars(result))


@router.get("", response_model=list[PayoutItem])
def list_payouts(
    year: int = Query(..., ge=2024, le=2099),
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_FINALIZE)
    ),
):
    target = civil_year_to_target_academic_year(year)
    cycle = session.scalar(
        select(YearEndCycle).where(YearEndCycle.academic_year == target)
    )
    if cycle is None:
        return []
    items = session.scalars(
        select(SpecialBonusItem).where(
            SpecialBonusItem.year_end_cycle_id == cycle.id,
            SpecialBonusItem.bonus_type.in_(
                [
                    SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                    SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
                ]
            ),
        )
    ).all()
    return [
        PayoutItem(
            id=i.id,
            employee_id=i.employee_id,
            bonus_type=i.bonus_type.value,
            period_label=i.period_label,
            amount=i.amount,
            source_ref=i.source_ref,
            calc_meta=i.calc_meta,
        )
        for i in items
    ]


@router.delete("/{year}")
def delete_payouts(
    year: int,
    confirm: bool = Query(False),
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_FINALIZE)
    ),
):
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="confirm=true required"
        )
    deleted = void_payouts(
        session,
        payout_year=year,
        voided_by=current_user.get("user_id", 0),
    )
    session.commit()
    return {"deleted_count": deleted}
