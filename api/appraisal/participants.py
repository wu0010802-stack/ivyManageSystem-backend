"""考核參與者 router。"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalCycle,
    AppraisalEvent,
    AppraisalParticipant,
    CycleStatus,
)
from models.database import get_session
from models.employee import Employee, JobTitle
from schemas.appraisal import (
    ParticipantBulkInit,
    ParticipantOut,
    ParticipantPatch,
)
from services.appraisal_service import (
    mark_summary_stale,
    suggest_role_group,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/cycles/{cycle_id}/participants", response_model=list[ParticipantOut])
def list_participants(
    cycle_id: int,
    db: Session = Depends(get_session),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    rows = (
        db.execute(
            select(AppraisalParticipant)
            .where(AppraisalParticipant.cycle_id == cycle_id)
            .order_by(AppraisalParticipant.role_group, AppraisalParticipant.employee_id)
        )
        .scalars()
        .all()
    )
    return rows


@router.post(
    "/cycles/{cycle_id}/participants:bulk_init",
    response_model=list[ParticipantOut],
)
def bulk_init_participants(
    cycle_id: int,
    payload: ParticipantBulkInit,
    request: Request,
    db: Session = Depends(get_session),
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    cycle = db.get(AppraisalCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    if cycle.status == CycleStatus.CLOSED:
        raise HTTPException(400, "cycle_closed")

    existing = {
        p.employee_id
        for p in db.execute(
            select(AppraisalParticipant).where(
                AppraisalParticipant.cycle_id == cycle_id
            )
        )
        .scalars()
        .all()
    }

    emp_stmt = select(Employee)
    if payload.employee_ids:
        emp_stmt = emp_stmt.where(Employee.id.in_(payload.employee_ids))
    # 未指定 employee_ids 時預設帶全體在職員工
    else:
        emp_stmt = emp_stmt.where(Employee.is_active == True)  # noqa: E712

    employees = db.execute(emp_stmt).scalars().all()

    created_count = 0
    for emp in employees:
        # 排除已建檔
        if emp.id in existing:
            continue
        # 排除不在職：is_active=False
        if not emp.is_active:
            continue
        # 排除在週期開始前已離職（resign_date < cycle.start_date）
        if emp.resign_date is not None and emp.resign_date < cycle.start_date:
            continue

        # 取職稱推薦 role_group（優先使用 job_title_rel，fallback 到 legacy title）
        title_name: Optional[str] = None
        if emp.job_title_rel is not None:
            title_name = emp.job_title_rel.name
        elif emp.job_title_id is not None:
            jt = db.get(JobTitle, emp.job_title_id)
            if jt:
                title_name = jt.name

        p = AppraisalParticipant(
            cycle_id=cycle_id,
            employee_id=emp.id,
            role_group=suggest_role_group(title_name),
            classroom_id=getattr(emp, "classroom_id", None),
            base_score=0,
        )
        db.add(p)
        created_count += 1

    db.commit()

    request.state.audit_entity_id = cycle_id
    request.state.audit_changes = {"created_count": created_count}

    rows = (
        db.execute(
            select(AppraisalParticipant)
            .where(AppraisalParticipant.cycle_id == cycle_id)
            .order_by(AppraisalParticipant.role_group, AppraisalParticipant.employee_id)
        )
        .scalars()
        .all()
    )
    return rows


@router.get("/participants/{participant_id}", response_model=ParticipantOut)
def get_participant(
    participant_id: int,
    db: Session = Depends(get_session),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    p = db.get(AppraisalParticipant, participant_id)
    if not p:
        raise HTTPException(404, "participant_not_found")
    return p


@router.patch("/participants/{participant_id}", response_model=ParticipantOut)
def patch_participant(
    participant_id: int,
    payload: ParticipantPatch,
    request: Request,
    db: Session = Depends(get_session),
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    p = db.get(AppraisalParticipant, participant_id)
    if not p:
        raise HTTPException(404, "participant_not_found")
    cycle = db.get(AppraisalCycle, p.cycle_id)
    if cycle.status == CycleStatus.CLOSED:
        raise HTTPException(400, "cycle_closed")

    changed_base_score = False
    if payload.role_group is not None:
        p.role_group = payload.role_group
    if payload.classroom_id is not None:
        p.classroom_id = payload.classroom_id
    if payload.base_score is not None:
        p.base_score = payload.base_score
        changed_base_score = True
    if payload.target_enrollment is not None:
        p.target_enrollment = payload.target_enrollment
    if payload.actual_enrollment is not None:
        p.actual_enrollment = payload.actual_enrollment

    db.flush()
    if changed_base_score:
        try:
            mark_summary_stale(db, p.id)
        except PermissionError as e:
            db.rollback()
            raise HTTPException(409, str(e))

    db.commit()
    db.refresh(p)
    request.state.audit_entity_id = p.id
    return p


@router.delete("/participants/{participant_id}", status_code=204)
def delete_participant(
    participant_id: int,
    request: Request,
    db: Session = Depends(get_session),
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    p = db.get(AppraisalParticipant, participant_id)
    if not p:
        raise HTTPException(404, "participant_not_found")
    has_event = db.execute(
        select(AppraisalEvent.id)
        .where(AppraisalEvent.participant_id == participant_id)
        .limit(1)
    ).scalar_one_or_none()
    if has_event:
        raise HTTPException(409, "participant_has_events:無法刪除有事件的參與者")
    db.delete(p)
    db.commit()
    request.state.audit_entity_id = participant_id
    return
