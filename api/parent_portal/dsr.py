"""api/parent_portal/dsr.py — 家長 DSR 五權之 delete / correct / opt-out（個資法 §3）。

P0c-2 落地三個申請類 endpoint：建立後進 dsr_requests pending queue，
admin review 決議後才實際觸發 lifecycle / business 流程（保學生稅務 7 年）。

Endpoints (家長端 /api/parent prefix):
- POST /me/delete-request   申請刪除（admin 決議 → 觸發 student_lifecycle 既有 GC 機制）
- POST /me/correct-request  申請更正欄位 + 新值 + 理由（admin 決議 → apply diff）
- POST /me/opt-out          申請停止特定 scope 處理（admin 決議 → 寫 consent_log consented=false
                            並可能 cascade 關閉對應 push 等）
- GET  /me/dsr-requests     查自己歷次申請狀態

家長一個 user_id 同類型最多 1 筆 pending（防垃圾轟炸 admin queue）。

Refs: docs/superpowers/specs/2026-05-28-consent-dsr-rights-design.md §3.2
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from models.consent import (
    CONSENT_SCOPE_SERVICE_ESSENTIAL,
    CONSENT_SCOPES,
    ParentConsentLog,
)
from models.dsr import (
    DSR_REQUEST_TYPE_CORRECT,
    DSR_REQUEST_TYPE_DELETE,
    DSR_REQUEST_TYPE_OPT_OUT,
    DSR_STATUS_APPROVED,
    DSR_STATUS_PENDING,
    DsrRequest,
)
from services.consent.checker import invalidate_consent_cache
from utils.audit import write_explicit_audit
from utils.auth import require_parent_role
from utils.request_ip import get_client_ip

from ._dependencies import get_parent_db
from ._shared import _get_parent_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["parent-dsr"])


# ── Schemas ────────────────────────────────────────────────────────────────


class DeleteRequestIn(BaseModel):
    subject_entity_type: str = Field(
        ..., description="刪除目標：'student' / 'guardian'（自己 = guardian）"
    )
    subject_entity_id: int = Field(..., description="目標 entity ID")
    reason: str = Field(..., min_length=5, description="申請刪除原因（≥5 字）")


class CorrectRequestIn(BaseModel):
    subject_entity_type: str = Field(..., description="'student' / 'guardian'")
    subject_entity_id: int = Field(..., description="目標 entity ID")
    field_name: str = Field(..., description="要更正的欄位名（如 phone / address）")
    new_value: str = Field(..., description="新值（字串化）")
    reason: str = Field(..., min_length=5, description="更正理由（≥5 字）")


class OptOutRequestIn(BaseModel):
    scope: str = Field(..., description="要停止處理的 scope（對齊 consent scope）")
    reason: Optional[str] = None


class DsrRequestOut(BaseModel):
    id: int
    request_type: str
    status: str
    subject_entity_type: Optional[str]
    subject_entity_id: Optional[int]
    field_name: Optional[str]
    scope: Optional[str]
    reason: Optional[str]
    submitted_at: str
    decided_at: Optional[str]
    decision_note: Optional[str]


# ── Helper：限制每類同時 1 pending ──


def _block_if_pending_same_type(session: Session, user_id: int, request_type: str):
    existing = (
        session.query(DsrRequest)
        .filter(
            DsrRequest.user_id == user_id,
            DsrRequest.request_type == request_type,
            DsrRequest.status == DSR_STATUS_PENDING,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"已有 pending {request_type} 申請（id={existing.id}），請等候園所處理或聯絡客服取消",
        )


def _to_out(req: DsrRequest) -> DsrRequestOut:
    return DsrRequestOut(
        id=req.id,
        request_type=req.request_type,
        status=req.status,
        subject_entity_type=req.subject_entity_type,
        subject_entity_id=req.subject_entity_id,
        field_name=req.field_name,
        scope=req.scope,
        reason=req.reason,
        submitted_at=req.submitted_at.isoformat(),
        decided_at=req.decided_at.isoformat() if req.decided_at else None,
        decision_note=req.decision_note,
    )


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/me/delete-request", response_model=DsrRequestOut)
def submit_delete_request(
    payload: DeleteRequestIn,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
) -> DsrRequestOut:
    """個資法 §3.5 刪除權：申請刪除子女或自己資料。

    送出 → pending queue → admin review。
    *不立即硬刪* — 學生資料涉合班/出席/費用稽核需保留稅務 7 年。
    """
    user = _get_parent_user(session, current_user)
    _block_if_pending_same_type(session, user.id, DSR_REQUEST_TYPE_DELETE)

    if payload.subject_entity_type not in ("student", "guardian"):
        raise HTTPException(status_code=400, detail="刪除目標僅支援 student / guardian")

    req = DsrRequest(
        user_id=user.id,
        request_type=DSR_REQUEST_TYPE_DELETE,
        status=DSR_STATUS_PENDING,
        subject_entity_type=payload.subject_entity_type,
        subject_entity_id=payload.subject_entity_id,
        reason=payload.reason,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    session.add(req)
    session.flush()

    write_explicit_audit(
        request,
        action="CREATE",
        entity_type="dsr_request",
        entity_id=str(req.id),
        summary=f"家長提交刪除申請（type={payload.subject_entity_type}/{payload.subject_entity_id}）",
        changes={
            "request_type": "delete",
            "subject_entity_type": payload.subject_entity_type,
            "subject_entity_id": payload.subject_entity_id,
        },
    )
    logger.info(
        "DSR delete-request: user_id=%s subject=%s/%s",
        user.id,
        payload.subject_entity_type,
        payload.subject_entity_id,
    )
    return _to_out(req)


@router.post("/me/correct-request", response_model=DsrRequestOut)
def submit_correct_request(
    payload: CorrectRequestIn,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
) -> DsrRequestOut:
    """個資法 §3.2 補充更正權：申請更正欄位 + 新值 + 理由。

    *不立即更新欄位* — admin review 後執行（避免家長直接改身分證等敏感欄位 IDOR）。
    """
    user = _get_parent_user(session, current_user)
    _block_if_pending_same_type(session, user.id, DSR_REQUEST_TYPE_CORRECT)

    if payload.subject_entity_type not in ("student", "guardian"):
        raise HTTPException(status_code=400, detail="更正目標僅支援 student / guardian")

    req = DsrRequest(
        user_id=user.id,
        request_type=DSR_REQUEST_TYPE_CORRECT,
        status=DSR_STATUS_PENDING,
        subject_entity_type=payload.subject_entity_type,
        subject_entity_id=payload.subject_entity_id,
        field_name=payload.field_name,
        new_value=payload.new_value,
        reason=payload.reason,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    session.add(req)
    session.flush()

    write_explicit_audit(
        request,
        action="CREATE",
        entity_type="dsr_request",
        entity_id=str(req.id),
        summary=f"家長提交更正申請（field={payload.field_name}）",
        changes={
            "request_type": "correct",
            "subject_entity_type": payload.subject_entity_type,
            "field_name": payload.field_name,
        },
    )
    return _to_out(req)


@router.post("/me/opt-out", response_model=DsrRequestOut)
def submit_opt_out_request(
    payload: OptOutRequestIn,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
) -> DsrRequestOut:
    """個資法 §3.3 停止處理利用權：granular scope 即時撤回（spec §3.2a）。

    - service_essential → 拒絕（400）：基礎服務同意不可停止，如需終止服務請走刪除申請。
    - granular scope（photo_publish / line_push / cross_border_transfer）→ 即時生效：
      1. 查該家長該 scope 最近一筆 ParentConsentLog 取 policy_version_id。
         若從未有任何記錄 → 400「無可撤回的同意紀錄」。
      2. 寫入 ParentConsentLog(consented=False, policy_version_id=<原版本>)。
      3. invalidate_consent_cache 立即清快取，下次查詢即時反映撤回。
      4. 寫 DsrRequest(status=approved) 作法律備案，不進 pending queue。
    """
    user = _get_parent_user(session, current_user)

    if payload.scope not in CONSENT_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"未知 scope: {payload.scope}; 允許: {sorted(CONSENT_SCOPES)}",
        )

    # service_essential 不可 opt-out
    if payload.scope == CONSENT_SCOPE_SERVICE_ESSENTIAL:
        raise HTTPException(
            status_code=400,
            detail=(
                "基礎服務同意不可停止；如需終止服務請走刪除申請（POST /me/delete-request）"
            ),
        )

    # 查最近一筆 ParentConsentLog 取 policy_version_id（任意 consented 值皆可）
    latest_log = (
        session.query(ParentConsentLog)
        .filter(
            ParentConsentLog.user_id == user.id,
            ParentConsentLog.scope == payload.scope,
        )
        .order_by(ParentConsentLog.consented_at.desc())
        .first()
    )
    if latest_log is None:
        raise HTTPException(
            status_code=400,
            detail=f"無可撤回的同意紀錄（scope={payload.scope}）",
        )

    policy_version_id = latest_log.policy_version_id

    # 即時寫入撤回 log
    revoke_log = ParentConsentLog(
        user_id=user.id,
        policy_version_id=policy_version_id,
        scope=payload.scope,
        consented=False,
        note=payload.reason,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    session.add(revoke_log)
    session.flush()

    # 立即清快取（確保下次 consent_check 重新讀 DB）
    invalidate_consent_cache(user.id, payload.scope)

    # 法律備案：DsrRequest status=approved（自助撤回不需 admin 核准）
    req = DsrRequest(
        user_id=user.id,
        request_type=DSR_REQUEST_TYPE_OPT_OUT,
        status=DSR_STATUS_APPROVED,
        scope=payload.scope,
        reason=payload.reason,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    session.add(req)
    session.flush()

    write_explicit_audit(
        request,
        action="CREATE",
        entity_type="dsr_request",
        entity_id=str(req.id),
        summary=f"家長 opt-out 即時撤回（scope={payload.scope}）",
        changes={"request_type": "opt_out", "scope": payload.scope, "immediate": True},
    )
    logger.info(
        "DSR opt-out immediate revoke: user_id=%s scope=%s",
        user.id,
        payload.scope,
    )
    return _to_out(req)


@router.get("/me/dsr-requests", response_model=list[DsrRequestOut])
def list_my_dsr_requests(
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
) -> list[DsrRequestOut]:
    """查自己歷次 DSR 申請（含 pending + 已決議），最新在前。"""
    user = _get_parent_user(session, current_user)
    rows = (
        session.query(DsrRequest)
        .filter(DsrRequest.user_id == user.id)
        .order_by(desc(DsrRequest.submitted_at))
        .all()
    )
    return [_to_out(r) for r in rows]
