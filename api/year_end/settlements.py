"""年終 settlement router：計算單一員工、批次計算、核定發放。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.database import get_session_dep
from models.year_end import (
    SettlementStatus,
    YearEndClassTarget,
    YearEndCycle,
    YearEndEmployeeSnapshot,
    YearEndOrgSettings,
    YearEndSettlement,
)
from schemas.year_end import (
    SettlementCalculate,
    SettlementFinalize,
    SettlementOut,
)
from services.year_end import settle_employee
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/cycles/{cycle_id}/settlements", response_model=list[SettlementOut]
)
def list_settlements(
    cycle_id: int,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_READ)),
):
    return (
        db.execute(
            select(YearEndSettlement).where(
                YearEndSettlement.cycle_id == cycle_id
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/cycles/{cycle_id}/settlements:calculate",
    response_model=SettlementOut,
)
def calculate_settlement(
    cycle_id: int,
    payload: SettlementCalculate,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_WRITE)),
):
    cycle = db.get(YearEndCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    snapshot = db.get(YearEndEmployeeSnapshot, payload.snapshot_id)
    if not snapshot or snapshot.cycle_id != cycle_id:
        raise HTTPException(404, "snapshot_not_found")

    org_settings = db.execute(
        select(YearEndOrgSettings).where(YearEndOrgSettings.cycle_id == cycle_id)
    ).scalar_one_or_none()
    if not org_settings:
        raise HTTPException(400, "org_settings_required:請先設定 org_settings")

    class_target = None
    if snapshot.classroom_id is not None:
        class_target = db.execute(
            select(YearEndClassTarget).where(
                YearEndClassTarget.cycle_id == cycle_id,
                YearEndClassTarget.classroom_id == snapshot.classroom_id,
            )
        ).scalar_one_or_none()

    deductions = {
        "late": payload.deductions.late,
        "personal_leave": payload.deductions.personal_leave,
        "sick_leave": payload.deductions.sick_leave,
        "meeting": payload.deductions.meeting,
        "disciplinary": payload.deductions.disciplinary,
        "parental_leave": payload.deductions.parental_leave,
    }

    try:
        settlement = settle_employee(
            db,
            cycle=cycle,
            snapshot=snapshot,
            org_settings=org_settings,
            class_target=class_target,
            deductions=deductions,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except PermissionError as e:
        raise HTTPException(409, str(e))

    settlement.calculated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settlement)
    request.state.audit_entity_id = settlement.id
    return settlement


@router.post(
    "/settlements/{settlement_id}/finalize", response_model=SettlementOut
)
def finalize_settlement(
    settlement_id: int,
    payload: SettlementFinalize,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.YEAR_END_FINALIZE)
    ),
):
    s = db.get(YearEndSettlement, settlement_id)
    if not s:
        raise HTTPException(404, "settlement_not_found")
    if s.status == SettlementStatus.FINALIZED:
        return s  # idempotent
    if s.status not in (SettlementStatus.CALCULATED, SettlementStatus.REVIEWED):
        raise HTTPException(
            400, f"status_invalid:{s.status.value}"
        )
    s.status = SettlementStatus.FINALIZED
    s.finalized_at = datetime.now(timezone.utc)
    s.finalized_by = current_user.get("user_id")
    db.commit()
    db.refresh(s)
    logger.warning(
        "[year_end] finalize settlement=%d user=%s reason=%r",
        s.id,
        current_user.get("user_id"),
        payload.reason,
    )
    request.state.audit_entity_id = s.id
    request.state.audit_changes = {"reason": payload.reason}
    return s
