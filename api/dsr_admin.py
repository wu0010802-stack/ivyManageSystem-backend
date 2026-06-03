"""api/dsr_admin.py — admin DSR 請求佇列（個資法權利請求管理，P2-3）。

DSR_MANAGE 權限守衛。提供 list / reject / approve。
approve 的 delete→lifecycle GC / correct→手動更正執行為 Task 12；本檔 approve 僅做
status 更新 + decision_note + audit（見 approve 內 TODO）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from models.database import get_session_dep
from models.dsr import (
    DSR_STATUS_APPROVED,
    DSR_STATUS_PENDING,
    DSR_STATUS_REJECTED,
    DsrRequest,
)
from schemas.dsr import DsrDecisionIn, DsrRequestAdminOut
from utils.audit import write_explicit_audit
from utils.auth import require_permission
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
    _: dict = Depends(require_permission(Permission.DSR_MANAGE)),
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
    current_user: dict = Depends(require_permission(Permission.DSR_MANAGE)),
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
    current_user: dict = Depends(require_permission(Permission.DSR_MANAGE)),
):
    """核准 pending DSR 請求。

    本端點僅做 status=approved + decision_note + audit。
    TODO(Task 12): 按 request_type 分派 delete→student_lifecycle 終態→365d GC /
    correct→admin 手動更正（不自動套用 new_value）；approve 前 ownership 重驗。
    """
    req = session.query(DsrRequest).filter(DsrRequest.id == req_id).first()
    if req is None or req.status != DSR_STATUS_PENDING:
        raise HTTPException(status_code=404, detail="申請不存在或已決議")
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
