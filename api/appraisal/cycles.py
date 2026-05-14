"""考核學期週期 router。

Import 路徑慣例（grep 驗證）：
  - DB session:      from models.database import get_session_dep  （re-export from models.base）
  - 權限守衛:        from utils.auth import require_staff_permission（管理端限定）
  - 當前使用者:      require_staff_permission 回傳 current_user dict
  - Session 型別:    from sqlalchemy.orm import Session

get_session_dep() 是 generator（yield + finally close），FastAPI Depends 自動 cleanup。
切勿改用 get_session()（普通回傳值），那會造成 session 洩漏撐爆連線池。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalCycle,
    AppraisalSummary,
    CycleStatus,
    SummaryStatus,
)
from models.database import get_session_dep
from schemas.appraisal import (
    CycleCreate,
    CycleOut,
    CyclePatch,
    CycleUnlockRequest,
)
from services.appraisal_service import default_cycle_dates
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/cycles", response_model=list[CycleOut])
def list_cycles(
    status_filter: Optional[CycleStatus] = Query(default=None, alias="status"),
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    """列出所有考核學期週期，可依 status 篩選。"""
    stmt = select(AppraisalCycle).order_by(
        AppraisalCycle.academic_year.desc(), AppraisalCycle.semester
    )
    if status_filter is not None:
        stmt = stmt.where(AppraisalCycle.status == status_filter)
    return db.execute(stmt).scalars().all()


@router.post("/cycles", response_model=CycleOut, status_code=status.HTTP_201_CREATED)
def create_cycle(
    payload: CycleCreate,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    """建立新的考核學期週期；start_date / end_date / base_score_calc_date 依學年自動帶入。

    同一學年 + 學期不允許重複（DB UNIQUE 約束，回 409）。
    """
    start, end, calc = default_cycle_dates(payload.academic_year, payload.semester)
    cycle = AppraisalCycle(
        academic_year=payload.academic_year,
        semester=payload.semester,
        start_date=start,
        end_date=end,
        base_score_calc_date=payload.base_score_calc_date or calc,
        status=CycleStatus.OPEN,
        created_by=current_user["user_id"],
    )
    db.add(cycle)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cycle_already_exists:該學年/學期已建立",
        )
    db.refresh(cycle)
    return cycle


@router.patch("/cycles/{cycle_id}", response_model=CycleOut)
def patch_cycle(
    cycle_id: int,
    payload: CyclePatch,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    """修改週期設定（目前僅允許調整 base_score_calc_date）。CLOSED 週期不可修改。"""
    cycle = db.get(AppraisalCycle, cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail="cycle_not_found")
    if cycle.status == CycleStatus.CLOSED:
        raise HTTPException(status_code=400, detail="cycle_closed:已封存週期不可修改")
    if payload.base_score_calc_date is not None:
        cycle.base_score_calc_date = payload.base_score_calc_date
    db.commit()
    db.refresh(cycle)
    return cycle


@router.post("/cycles/{cycle_id}/lock", response_model=CycleOut)
def lock_cycle(
    cycle_id: int,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_FINALIZE)
    ),
):
    """將週期從 OPEN 鎖定為 LOCKED（僅 APPRAISAL_FINALIZE 權限）。

    LOCKED 後不可新增／修改事件；解鎖需 unlock endpoint + reason。
    audit_entity_id 覆寫讓 AuditMiddleware 記錄至正確資源。
    """
    cycle = db.get(AppraisalCycle, cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail="cycle_not_found")
    if cycle.status != CycleStatus.OPEN:
        raise HTTPException(
            status_code=400,
            detail=f"cycle_status_invalid:{cycle.status.value}",
        )
    cycle.status = CycleStatus.LOCKED
    db.commit()
    db.refresh(cycle)
    # AuditMiddleware 慣例：覆寫 audit_entity_id 讓 log 指向 cycle id
    request.state.audit_entity_id = cycle.id
    return cycle


@router.post("/cycles/{cycle_id}/unlock", response_model=CycleOut)
def unlock_cycle(
    cycle_id: int,
    payload: CycleUnlockRequest,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_FINALIZE)
    ),
):
    """將週期從 LOCKED 解鎖回 OPEN。reason 最少 4 字，透過 CycleUnlockRequest 強制。

    解鎖為高風險操作，額外記錄 WARNING log 與 audit_changes reason。
    """
    cycle = db.get(AppraisalCycle, cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail="cycle_not_found")
    if cycle.status != CycleStatus.LOCKED:
        raise HTTPException(
            status_code=400,
            detail=f"cycle_status_invalid:{cycle.status.value}",
        )
    cycle.status = CycleStatus.OPEN
    db.commit()
    db.refresh(cycle)
    logger.warning(
        "[appraisal] cycle %d 解鎖 by user=%s reason=%r",
        cycle.id,
        current_user.get("user_id"),
        payload.reason,
    )
    # AuditMiddleware 慣例
    request.state.audit_entity_id = cycle.id
    request.state.audit_changes = {"reason": payload.reason}
    return cycle


@router.post("/cycles/{cycle_id}/close", response_model=CycleOut)
def close_cycle(
    cycle_id: int,
    request: Request,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_FINALIZE)
    ),
):
    """封存週期（CLOSED）；要求所有 AppraisalSummary 均已達 FINALIZED 狀態。

    幂等：若已是 CLOSED 直接回傳，不重複 commit。
    狀態機：OPEN / LOCKED → CLOSED（無法反轉）。
    """
    cycle = db.get(AppraisalCycle, cycle_id)
    if not cycle:
        raise HTTPException(status_code=404, detail="cycle_not_found")
    if cycle.status == CycleStatus.CLOSED:
        # 幂等：已封存直接回傳
        return cycle

    # 確認所有 summary 均已 FINALIZED
    unfinalized_id = db.execute(
        select(AppraisalSummary.id)
        .where(
            AppraisalSummary.cycle_id == cycle_id,
            AppraisalSummary.status != SummaryStatus.FINALIZED,
        )
        .limit(1)
    ).scalar_one_or_none()
    if unfinalized_id is not None:
        raise HTTPException(
            status_code=400,
            detail="not_all_finalized:仍有 summary 未到 FINALIZED 狀態",
        )

    cycle.status = CycleStatus.CLOSED
    db.commit()
    db.refresh(cycle)
    request.state.audit_entity_id = cycle.id
    return cycle
