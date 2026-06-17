"""api/policies_admin.py — admin 隱私權政策版本管理（P2-2 補遺）。

DSR_MANAGE 權限守衛。提供 list / create。
建立 effective_at<=now 的新版即觸發既有家長下次 has_signed_current_policy 失效 → 重簽，
無需額外程式——純資料驅動。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from models.consent import PolicyVersion
from models.database import get_session_dep
from schemas.dsr import PolicyVersionAdminOut, PolicyVersionCreateIn
from utils.audit import write_explicit_audit
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/policies", tags=["policy-admin"])


def _to_out(pv: PolicyVersion) -> PolicyVersionAdminOut:
    return PolicyVersionAdminOut(
        id=pv.id,
        version=pv.version,
        effective_at=pv.effective_at.isoformat() if pv.effective_at else "",
        document_path=pv.document_path,
        summary=pv.summary,
        created_at=pv.created_at.isoformat() if pv.created_at else "",
    )


@router.get("", response_model=list[PolicyVersionAdminOut])
def list_policy_versions(
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_staff_permission(Permission.DSR_MANAGE)),
):
    """列出所有 PolicyVersion，effective_at desc（最新生效版排前）。"""
    rows = (
        session.query(PolicyVersion)
        .order_by(PolicyVersion.effective_at.desc(), PolicyVersion.id.desc())
        .all()
    )
    return [_to_out(pv) for pv in rows]


@router.post("", response_model=PolicyVersionAdminOut)
def create_policy_version(
    payload: PolicyVersionCreateIn,
    request: Request,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.DSR_MANAGE)),
):
    """建立新 PolicyVersion。

    - version 唯一，重複 version → 409。
    - effective_at 解析為 naive datetime（isoformat 輸入）。
    - 建立 effective_at <= now 的新版即觸發既有家長下次
      has_signed_current_policy 失效 → 重簽（純資料驅動，此端點不額外處理）。
    """
    # 1. 唯一性前置檢查（避免 IntegrityError 污染 session）
    existing = (
        session.query(PolicyVersion)
        .filter(PolicyVersion.version == payload.version)
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"policy version '{payload.version}' 已存在，請使用不同版本號",
        )

    # 2. 解析 effective_at（isoformat → naive datetime）
    try:
        effective_at = datetime.fromisoformat(payload.effective_at)
        # 若含時區資訊則剝除（column 為 naive DateTime，比較使用 now_taipei_naive()）
        if effective_at.tzinfo is not None:
            effective_at = effective_at.replace(tzinfo=None)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"effective_at 格式錯誤，需為 isoformat datetime 字串：{exc}",
        )

    # 3. 建立並 flush（取得 id 後才能寫 audit）
    pv = PolicyVersion(
        version=payload.version,
        effective_at=effective_at,
        document_path=payload.document_path,
        summary=payload.summary,
    )
    session.add(pv)
    session.flush()  # 取得 pv.id

    # 4. 稽核軌跡
    write_explicit_audit(
        request,
        action="CREATE",
        entity_type="policy_version",
        entity_id=str(pv.id),
        summary=f"建立 PolicyVersion {pv.version}（effective_at={effective_at.isoformat()}）",
        changes={
            "version": pv.version,
            "effective_at": effective_at.isoformat(),
            "document_path": pv.document_path,
        },
    )

    session.commit()
    session.refresh(pv)
    return _to_out(pv)
