"""api/dsr_admin.py — admin DSR 請求佇列（個資法權利請求管理，P2-3）。

DSR_MANAGE 權限守衛。提供 list / reject / approve。
approve 按 request_type 分派（Task 12）：
  - delete → ownership 重驗 + student_lifecycle.transition → WITHDRAWN → 365d GC 接手
  - correct → 僅記 status=approved，admin 事後手動更正
  - 其他 → 防禦性僅 set status
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from models.classroom import LIFECYCLE_WITHDRAWN, Student
from models.database import get_session_dep
from models.dsr import (
    DSR_REQUEST_TYPE_DELETE,
    DSR_STATUS_APPROVED,
    DSR_STATUS_PENDING,
    DSR_STATUS_REJECTED,
    DsrRequest,
)
from models.guardian import Guardian
from schemas.dsr import DsrDecisionIn, DsrRequestAdminOut
from services.student_lifecycle import LifecycleTransitionError, transition
from utils.audit import write_explicit_audit
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.taipei_time import now_taipei_naive

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/dsr-requests", tags=["dsr-admin"])


def _to_admin_out(req: DsrRequest) -> DsrRequestAdminOut:
    return DsrRequestAdminOut(
        id=req.id,
        user_id=req.user_id,
        request_type=req.request_type,
        status=req.status,
        subject_entity_type=req.subject_entity_type,
        subject_entity_id=req.subject_entity_id,
        scope=req.scope,
        field_name=req.field_name,
        new_value=req.new_value,
        reason=req.reason,
        submitted_at=req.submitted_at.isoformat() if req.submitted_at else "",
        decided_at=req.decided_at.isoformat() if req.decided_at else None,
        decided_by=req.decided_by,
        decision_note=req.decision_note,
    )


@router.get("", response_model=list[DsrRequestAdminOut])
def list_dsr_requests(
    status: str | None = None,
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_staff_permission(Permission.DSR_MANAGE)),
):
    """列出 DSR 請求（可選 status filter），submitted_at desc。"""
    q = session.query(DsrRequest)
    if status:
        q = q.filter(DsrRequest.status == status)
    rows = q.order_by(DsrRequest.submitted_at.desc(), DsrRequest.id.desc()).all()
    return [_to_admin_out(r) for r in rows]


@router.post("/{req_id}/reject", response_model=DsrRequestAdminOut)
def reject_dsr_request(
    req_id: int,
    payload: DsrDecisionIn,
    request: Request,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.DSR_MANAGE)),
):
    """駁回 pending DSR 請求 → status=rejected + decision_note + audit。"""
    req = session.query(DsrRequest).filter(DsrRequest.id == req_id).first()
    if req is None or req.status != DSR_STATUS_PENDING:
        raise HTTPException(status_code=404, detail="申請不存在或已決議")
    req.status = DSR_STATUS_REJECTED
    req.decided_at = now_taipei_naive()
    req.decided_by = current_user["user_id"]
    req.decision_note = payload.decision_note
    write_explicit_audit(
        request,
        action="UPDATE",
        entity_type="dsr_request",
        entity_id=str(req.id),
        summary=f"DSR 駁回（{req.request_type}）",
        changes={"status": "rejected"},
    )
    session.commit()
    return _to_admin_out(req)


@router.post("/{req_id}/approve", response_model=DsrRequestAdminOut)
def approve_dsr_request(
    req_id: int,
    payload: DsrDecisionIn,
    request: Request,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.DSR_MANAGE)),
):
    """核准 pending DSR 請求。

    按 request_type 分派（Task 12）：
    - delete：ownership 重驗 → student lifecycle → WITHDRAWN → 365d GC 接手 PII 抹除
    - correct：僅 status=approved；admin 事後透過既有編輯工具手動更正（不自動套用 new_value）
    - 其他（opt_out 等）：防禦性僅 set status（opt_out 已於 Task 9 即時自助不進 queue）
    """
    req = session.query(DsrRequest).filter(DsrRequest.id == req_id).first()
    if req is None or req.status != DSR_STATUS_PENDING:
        raise HTTPException(status_code=404, detail="申請不存在或已決議")

    # ── 按 request_type 分派執行 ──────────────────────────────────────────
    if req.request_type == DSR_REQUEST_TYPE_DELETE:
        _execute_delete_dsr(session, req, payload, request, current_user)
    # correct / opt_out / 其他：不自動套用，僅走後續 set status
    # ─────────────────────────────────────────────────────────────────────

    # 共用：set status + decided_* + decision_note + audit（單一 commit）
    req.status = DSR_STATUS_APPROVED
    req.decided_at = now_taipei_naive()
    req.decided_by = current_user["user_id"]
    req.decision_note = payload.decision_note
    write_explicit_audit(
        request,
        action="UPDATE",
        entity_type="dsr_request",
        entity_id=str(req.id),
        summary=f"DSR 核准（{req.request_type}）",
        changes={"status": "approved"},
    )
    session.commit()
    return _to_admin_out(req)


def _execute_delete_dsr(
    session: Session,
    req: DsrRequest,
    payload: DsrDecisionIn,
    request: Request,
    current_user: dict,
) -> None:
    """delete DSR 核准執行：ownership 重驗 → student lifecycle → WITHDRAWN。

    - Guardian PII 抹除由既有 365d GC scheduler 接手，本函式不直接刪除資料。
    - 出席/費用/薪資記錄屬法定保存，由 GC 既有邏輯處理，本端點不碰。
    - 成功後直接 return；失敗時 raise HTTPException（呼叫端不應再 commit）。
    """
    # 1. ownership 重驗：申請家長必須為 subject student 的現有監護人
    if req.subject_entity_type != "student":
        # 非學生主體的 delete DSR 目前不支援自動執行，守衛後回 200（人工處理）
        return

    guardian = (
        session.query(Guardian)
        .filter(
            Guardian.student_id == req.subject_entity_id,
            Guardian.user_id == req.user_id,
            Guardian.deleted_at.is_(None),
        )
        .first()
    )
    if guardian is None:
        raise HTTPException(
            status_code=403,
            detail="申請人與目標學生無監護關係，不可核准刪除",
        )

    # 2. 取 student；不存在 → 404
    student = session.query(Student).filter(Student.id == req.subject_entity_id).first()
    if student is None:
        raise HTTPException(status_code=404, detail="目標學生不存在")

    # 3. 走 lifecycle transition → WITHDRAWN（365d GC 接手抹除 Guardian PII）
    try:
        transition(
            session,
            student,
            to_status=LIFECYCLE_WITHDRAWN,
            reason="個資法 DSR 刪除申請",
            notes=payload.decision_note,
            recorded_by=current_user["user_id"],
            request=request,
        )
    except LifecycleTransitionError as e:
        raise HTTPException(status_code=400, detail=str(e))
