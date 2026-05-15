"""年終獎金週期 + org settings + class targets + employee snapshots router。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.database import get_session_dep
from models.year_end import (
    YearEndClassTarget,
    YearEndCycle,
    YearEndCycleStatus,
    YearEndEmployeeSnapshot,
    YearEndOrgSettings,
)
from schemas.year_end import (
    ClassTargetOut,
    ClassTargetUpsert,
    EmployeeSnapshotOut,
    EmployeeSnapshotUpsert,
    OrgSettingsOut,
    OrgSettingsUpsert,
    YearEndCycleCreate,
    YearEndCycleOut,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/cycles", response_model=list[YearEndCycleOut])
def list_cycles(
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_READ)),
):
    return (
        db.execute(
            select(YearEndCycle).order_by(YearEndCycle.academic_year.desc())
        )
        .scalars()
        .all()
    )


@router.post(
    "/cycles",
    response_model=YearEndCycleOut,
    status_code=status.HTTP_201_CREATED,
)
def create_cycle(
    payload: YearEndCycleCreate,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_WRITE)),
):
    cycle = YearEndCycle(
        academic_year=payload.academic_year,
        status=YearEndCycleStatus.DRAFT,
        created_by=current_user.get("user_id"),
    )
    db.add(cycle)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "cycle_already_exists:該學年已建立")
    db.refresh(cycle)
    return cycle


# ── Org Settings ────────────────────────────────────────────────────────────


@router.get("/cycles/{cycle_id}/org_settings", response_model=OrgSettingsOut)
def get_org_settings(
    cycle_id: int,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_READ)),
):
    s = db.execute(
        select(YearEndOrgSettings).where(YearEndOrgSettings.cycle_id == cycle_id)
    ).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "org_settings_not_found")
    return s


@router.put("/cycles/{cycle_id}/org_settings", response_model=OrgSettingsOut)
def upsert_org_settings(
    cycle_id: int,
    payload: OrgSettingsUpsert,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_WRITE)),
):
    cycle = db.get(YearEndCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    if cycle.status == YearEndCycleStatus.PAID:
        raise HTTPException(400, "cycle_paid:已發放週期不可修改")

    existing = db.execute(
        select(YearEndOrgSettings).where(YearEndOrgSettings.cycle_id == cycle_id)
    ).scalar_one_or_none()
    if existing is None:
        existing = YearEndOrgSettings(cycle_id=cycle_id)
        db.add(existing)

    for field in (
        "total_enrollment_target",
        "achievement_rate_first",
        "achievement_rate_second",
        "org_achievement_rate",
        "festival_bonus_total_amount",
        "org_meeting_deduction",
        "extras_json",
    ):
        setattr(existing, field, getattr(payload, field))

    db.commit()
    db.refresh(existing)
    request.state.audit_entity_id = cycle_id
    return existing


# ── Class Targets ───────────────────────────────────────────────────────────


@router.get(
    "/cycles/{cycle_id}/class_targets", response_model=list[ClassTargetOut]
)
def list_class_targets(
    cycle_id: int,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_READ)),
):
    return (
        db.execute(
            select(YearEndClassTarget).where(
                YearEndClassTarget.cycle_id == cycle_id
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/cycles/{cycle_id}/class_targets",
    response_model=ClassTargetOut,
    status_code=201,
)
def upsert_class_target(
    cycle_id: int,
    payload: ClassTargetUpsert,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_WRITE)),
):
    cycle = db.get(YearEndCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")

    existing = db.execute(
        select(YearEndClassTarget).where(
            YearEndClassTarget.cycle_id == cycle_id,
            YearEndClassTarget.classroom_id == payload.classroom_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = YearEndClassTarget(
            cycle_id=cycle_id, classroom_id=payload.classroom_id
        )
        db.add(existing)
    for field in (
        "staffing_target",
        "achievement_rate_first",
        "achievement_rate_second",
        "returning_rate_first",
        "returning_rate_second",
    ):
        setattr(existing, field, getattr(payload, field))
    db.commit()
    db.refresh(existing)
    request.state.audit_entity_id = existing.id
    return existing


# ── Employee Snapshots ──────────────────────────────────────────────────────


@router.get(
    "/cycles/{cycle_id}/snapshots", response_model=list[EmployeeSnapshotOut]
)
def list_snapshots(
    cycle_id: int,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_READ)),
):
    return (
        db.execute(
            select(YearEndEmployeeSnapshot).where(
                YearEndEmployeeSnapshot.cycle_id == cycle_id
            )
        )
        .scalars()
        .all()
    )


@router.post(
    "/cycles/{cycle_id}/snapshots",
    response_model=EmployeeSnapshotOut,
    status_code=201,
)
def upsert_snapshot(
    cycle_id: int,
    payload: EmployeeSnapshotUpsert,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_WRITE)),
):
    cycle = db.get(YearEndCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    if cycle.status == YearEndCycleStatus.PAID:
        raise HTTPException(400, "cycle_paid:已發放週期不可修改")

    existing = db.execute(
        select(YearEndEmployeeSnapshot).where(
            YearEndEmployeeSnapshot.cycle_id == cycle_id,
            YearEndEmployeeSnapshot.employee_id == payload.employee_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = YearEndEmployeeSnapshot(
            cycle_id=cycle_id, employee_id=payload.employee_id
        )
        db.add(existing)
    for field in (
        "base_salary",
        "festival_total",
        "role_group",
        "hire_date",
        "classroom_id",
        "is_resigned",
        "resign_date",
        "is_contracted",
    ):
        setattr(existing, field, getattr(payload, field))
    db.commit()
    db.refresh(existing)
    request.state.audit_entity_id = existing.id
    return existing
