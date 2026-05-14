"""考核 summary 與三階簽核 router。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalSummary,
    SummaryStatus,
)
from models.database import get_session_dep
from schemas.appraisal import (
    FinalizeRequest,
    RejectRequest,
    SignRequest,
    SummaryOut,
)
from services.appraisal_service import (
    load_active_rates_map,
    recompute_summary,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission, has_permission

logger = logging.getLogger(__name__)
router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_stage(summary: AppraisalSummary, expected: SummaryStatus) -> None:
    if summary.status != expected:
        raise HTTPException(
            400,
            f"stage_invalid:expected={expected.value} got={summary.status.value}",
        )


@router.get("/summaries", response_model=list[SummaryOut])
def list_summaries(
    cycle_id: Optional[int] = None,
    status_filter: Optional[SummaryStatus] = None,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    stmt = select(AppraisalSummary).order_by(AppraisalSummary.id)
    if cycle_id is not None:
        stmt = stmt.where(AppraisalSummary.cycle_id == cycle_id)
    if status_filter is not None:
        stmt = stmt.where(AppraisalSummary.status == status_filter)
    return db.execute(stmt).scalars().all()


@router.get("/summaries/{summary_id}", response_model=SummaryOut)
def get_summary(
    summary_id: int,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    s = db.get(AppraisalSummary, summary_id)
    if not s:
        raise HTTPException(404, "summary_not_found")
    return s


@router.post(
    "/cycles/{cycle_id}/summaries:recompute",
    response_model=list[SummaryOut],
)
def recompute_cycle_summaries(
    cycle_id: int,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_REVIEW)),
):
    cycle = db.get(AppraisalCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")

    rates_map = load_active_rates_map(db, cycle.base_score_calc_date)

    participants = (
        db.execute(
            select(AppraisalParticipant).where(
                AppraisalParticipant.cycle_id == cycle_id
            )
        )
        .scalars()
        .all()
    )

    for p in participants:
        summary = db.execute(
            select(AppraisalSummary).where(AppraisalSummary.participant_id == p.id)
        ).scalar_one_or_none()
        if summary is None:
            summary = AppraisalSummary(
                participant_id=p.id,
                cycle_id=p.cycle_id,
                base_score=p.base_score,
                status=SummaryStatus.DRAFT,
            )
            db.add(summary)
            db.flush()
        if summary.status == SummaryStatus.FINALIZED:
            continue  # 跳過 finalized；其他重算
        recompute_summary(db, summary, p, rates_map)

    db.commit()

    rows = (
        db.execute(
            select(AppraisalSummary).where(AppraisalSummary.cycle_id == cycle_id)
        )
        .scalars()
        .all()
    )
    return rows


@router.post("/summaries/{summary_id}/sign_supervisor", response_model=SummaryOut)
def sign_supervisor(
    summary_id: int,
    payload: SignRequest,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_REVIEW)),
):
    s = db.get(AppraisalSummary, summary_id)
    if not s:
        raise HTTPException(404, "summary_not_found")
    _check_stage(s, SummaryStatus.DRAFT)
    s.status = SummaryStatus.SUPERVISOR_SIGNED
    s.supervisor_signed_at = _now()
    s.supervisor_signed_by = current_user["user_id"]
    s.supervisor_comment = payload.comment
    db.commit()
    db.refresh(s)
    request.state.audit_entity_id = s.id
    return s


@router.post("/summaries/{summary_id}/sign_accounting", response_model=SummaryOut)
def sign_accounting(
    summary_id: int,
    payload: SignRequest,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_ACCOUNTING)
    ),
):
    s = db.get(AppraisalSummary, summary_id)
    if not s:
        raise HTTPException(404, "summary_not_found")
    _check_stage(s, SummaryStatus.SUPERVISOR_SIGNED)
    s.status = SummaryStatus.ACCOUNTING_SIGNED
    s.accounting_signed_at = _now()
    s.accounting_signed_by = current_user["user_id"]
    s.accounting_comment = payload.comment
    db.commit()
    db.refresh(s)
    request.state.audit_entity_id = s.id
    return s


@router.post("/summaries/{summary_id}/finalize", response_model=SummaryOut)
def finalize_summary(
    summary_id: int,
    payload: FinalizeRequest,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_FINALIZE)
    ),
):
    s = db.get(AppraisalSummary, summary_id)
    if not s:
        raise HTTPException(404, "summary_not_found")
    _check_stage(s, SummaryStatus.ACCOUNTING_SIGNED)
    s.status = SummaryStatus.FINALIZED
    s.finalized_at = _now()
    s.finalized_by = current_user["user_id"]
    s.finalized_comment = payload.comment
    db.commit()
    db.refresh(s)
    logger.info(
        "[appraisal] finalize summary=%d user=%d reason=%r",
        s.id,
        current_user["user_id"],
        payload.reason,
    )
    request.state.audit_entity_id = s.id
    request.state.audit_changes = {"reason": payload.reason}
    return s


@router.post("/summaries/{summary_id}/reject", response_model=SummaryOut)
def reject_summary(
    summary_id: int,
    payload: RejectRequest,
    request: Request,
    db: Session = Depends(get_session_dep),
    # READ 守衛先擋；本 endpoint 再依 stage 動態檢查更高權限
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    """退回任一階段。權限依當前 stage 動態判定。

    SUPERVISOR_SIGNED → 要 APPRAISAL_ACCOUNTING 或更高
    ACCOUNTING_SIGNED → 要 APPRAISAL_FINALIZE
    FINALIZED 已封存，不可 reject（要先解封 cycle）
    """
    s = db.get(AppraisalSummary, summary_id)
    if not s:
        raise HTTPException(404, "summary_not_found")
    if s.status == SummaryStatus.DRAFT:
        raise HTTPException(400, "stage_invalid:already_draft")
    if s.status == SummaryStatus.FINALIZED:
        raise HTTPException(400, "stage_invalid:finalized_must_unlock_cycle")

    required_perm = (
        Permission.APPRAISAL_FINALIZE
        if s.status == SummaryStatus.ACCOUNTING_SIGNED
        else Permission.APPRAISAL_ACCOUNTING
    )
    user_perms = current_user.get("permissions", 0) or 0
    if not has_permission(user_perms, required_perm):
        raise HTTPException(403, f"missing_permission:{required_perm.name}")

    s.rejected_at = _now()
    s.rejected_by = current_user["user_id"]
    s.rejected_from_stage = s.status
    s.rejected_reason = payload.reason
    # 清簽核戳記
    s.status = SummaryStatus.DRAFT
    s.supervisor_signed_at = None
    s.supervisor_signed_by = None
    s.supervisor_comment = None
    s.accounting_signed_at = None
    s.accounting_signed_by = None
    s.accounting_comment = None
    db.commit()
    db.refresh(s)
    request.state.audit_entity_id = s.id
    request.state.audit_changes = {"reason": payload.reason}
    return s
