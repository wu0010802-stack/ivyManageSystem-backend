"""api/parent_portal/consent.py — 家長同意 / 政策版本（個資法 §8 / §19）。

P0c 法規/個資 sprint 第三件 Phase 1：consent log 寫入 + 查詢 + 政策版本查詢。

Endpoints:
- GET  /api/parent/me/consents   列當前家長各 scope 最新狀態 + history
- POST /api/parent/me/consent    寫入同意 / 撤回事件（同表，consented=true/false）
- GET  /api/policies/current     公開（require_parent_role）回當前生效 policy_version

Phase 1 scope:
- 不含 LIFF UI（前端另起 PR）
- 不含 admin upload policy（admin 管理端另起 PR）
- 不含「policy 升版強制重簽」攔截（middleware 另起 PR）

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
    CONSENT_SCOPES,
    ParentConsentLog,
    PolicyVersion,
)
from utils.auth import require_parent_role
from utils.request_ip import get_client_ip

from ._dependencies import get_parent_db
from ._shared import _get_parent_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["parent-consent"])


# ── Schemas ────────────────────────────────────────────────────────────────


class ConsentEventIn(BaseModel):
    policy_version_id: int = Field(..., description="同意基於哪個 policy_version")
    scope: str = Field(..., description="同意範疇")
    consented: bool = Field(..., description="true=同意, false=撤回")
    note: Optional[str] = Field(None, description="撤回理由")


class ConsentEventOut(BaseModel):
    id: int
    scope: str
    consented: bool
    consented_at: str
    policy_version_id: int


class ScopeStatusOut(BaseModel):
    scope: str
    consented: Optional[bool] = Field(None, description="None 表示未曾簽過")
    consented_at: Optional[str] = None
    policy_version_id: Optional[int] = None


class ConsentsResponseOut(BaseModel):
    current_status: list[ScopeStatusOut]
    history: list[ConsentEventOut]


class PolicyVersionOut(BaseModel):
    id: int
    version: str
    effective_at: str
    document_path: str
    summary: Optional[str]


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/me/consents", response_model=ConsentsResponseOut)
def list_my_consents(
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
) -> ConsentsResponseOut:
    """列當前家長各 scope 最新同意狀態 + 完整 history。"""
    user = _get_parent_user(session, current_user)

    # 全部 history（最新在前）
    logs = (
        session.query(ParentConsentLog)
        .filter(ParentConsentLog.user_id == user.id)
        .order_by(desc(ParentConsentLog.consented_at))
        .all()
    )

    # 各 scope 最新狀態（取最新一筆）
    seen: dict[str, ParentConsentLog] = {}
    for log in logs:
        if log.scope not in seen:
            seen[log.scope] = log

    current_status = [
        ScopeStatusOut(
            scope=scope,
            consented=(seen[scope].consented if scope in seen else None),
            consented_at=(
                seen[scope].consented_at.isoformat() if scope in seen else None
            ),
            policy_version_id=(
                seen[scope].policy_version_id if scope in seen else None
            ),
        )
        for scope in sorted(CONSENT_SCOPES)
    ]

    history = [
        ConsentEventOut(
            id=log.id,
            scope=log.scope,
            consented=log.consented,
            consented_at=log.consented_at.isoformat(),
            policy_version_id=log.policy_version_id,
        )
        for log in logs
    ]

    return ConsentsResponseOut(current_status=current_status, history=history)


@router.post("/me/consent", response_model=ConsentEventOut)
def write_consent(
    payload: ConsentEventIn,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
) -> ConsentEventOut:
    """寫入一次同意 / 撤回事件。同一 scope 多次寫入皆為獨立 log。"""
    if payload.scope not in CONSENT_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"未知 scope: {payload.scope}; 允許: {sorted(CONSENT_SCOPES)}",
        )

    # 確認 policy_version_id 存在
    policy = (
        session.query(PolicyVersion)
        .filter(PolicyVersion.id == payload.policy_version_id)
        .first()
    )
    if policy is None:
        raise HTTPException(status_code=400, detail="policy_version_id 不存在")

    user = _get_parent_user(session, current_user)

    log = ParentConsentLog(
        user_id=user.id,
        policy_version_id=payload.policy_version_id,
        scope=payload.scope,
        consented=payload.consented,
        ip_address=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        note=payload.note,
    )
    session.add(log)
    session.flush()

    logger.info(
        "parent_consent: user_id=%s scope=%s consented=%s policy_version=%s",
        user.id,
        payload.scope,
        payload.consented,
        policy.version,
    )

    return ConsentEventOut(
        id=log.id,
        scope=log.scope,
        consented=log.consented,
        consented_at=log.consented_at.isoformat(),
        policy_version_id=log.policy_version_id,
    )


@router.get("/policies/current", response_model=PolicyVersionOut)
def get_current_policy(
    # 為 Phase 1 簡化：仍要求 parent role；admin/public 路徑列為 follow-up
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
) -> PolicyVersionOut:
    """回當前生效的最新 policy_version。"""
    from utils.taipei_time import now_taipei_naive

    policy = (
        session.query(PolicyVersion)
        .filter(PolicyVersion.effective_at <= now_taipei_naive())
        .order_by(desc(PolicyVersion.effective_at))
        .first()
    )
    if policy is None:
        raise HTTPException(status_code=404, detail="尚無生效中的隱私權政策版本")

    return PolicyVersionOut(
        id=policy.id,
        version=policy.version,
        effective_at=policy.effective_at.isoformat(),
        document_path=policy.document_path,
        summary=policy.summary,
    )
