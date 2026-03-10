"""
審核流程設定 router（政策 CRUD + 簽核記錄查詢）
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from pydantic import BaseModel

from models.database import get_session, ApprovalPolicy, ApprovalLog
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["approval-settings"])

# 預設審核矩陣（申請人角色 → 可審核角色）
DEFAULT_POLICIES = [
    {"submitter_role": "teacher",    "approver_roles": "supervisor,hr,admin"},
    {"submitter_role": "supervisor", "approver_roles": "hr,admin"},
    {"submitter_role": "hr",         "approver_roles": "admin"},
    {"submitter_role": "admin",      "approver_roles": "admin"},
]

# 角色層級（數字越大層級越高）
ROLE_HIERARCHY = {
    "teacher":    1,
    "supervisor": 2,
    "hr":         3,
    "admin":      4,
}


# ============ Pydantic Models ============

class PolicyItem(BaseModel):
    submitter_role: str
    approver_roles: str  # 逗號分隔
    is_active: bool = True


class PolicyUpdateRequest(BaseModel):
    policies: List[PolicyItem]


# ============ Routes ============

@router.get("/approval-settings/policies")
def get_approval_policies(
    current_user: dict = Depends(require_permission(Permission.SETTINGS_READ)),
):
    """查詢全部審核政策（需 SETTINGS_READ 權限）"""
    session = get_session()
    try:
        policies = session.query(ApprovalPolicy).order_by(ApprovalPolicy.id).all()
        return [
            {
                "id": p.id,
                "doc_type": p.doc_type,
                "submitter_role": p.submitter_role,
                "approver_roles": p.approver_roles,
                "is_active": p.is_active,
            }
            for p in policies
        ]
    finally:
        session.close()


@router.put("/approval-settings/policies")
def update_approval_policies(
    body: PolicyUpdateRequest,
    current_user: dict = Depends(require_permission(Permission.SETTINGS_WRITE)),
):
    """批次更新審核政策（需 SETTINGS_WRITE / admin 權限）"""
    # 只有 admin 可以修改審核政策
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="僅系統管理員可修改審核流程政策")

    session = get_session()
    try:
        for item in body.policies:
            submitter_level = ROLE_HIERARCHY.get(item.submitter_role, 0)
            # 校驗：不允許授予低於申請人層級的角色作為審核人（admin 除外）
            for r in [r.strip() for r in item.approver_roles.split(",") if r.strip()]:
                if r not in ROLE_HIERARCHY:
                    raise HTTPException(
                        status_code=400,
                        detail=f"無效的角色名稱：{r}，允許值：teacher / supervisor / hr / admin",
                    )
                if r != "admin" and ROLE_HIERARCHY.get(r, 0) < submitter_level:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"審核者角色 {r} 層級不得低於申請人角色 {item.submitter_role}，"
                            "請確保審核流程由高層級角色向下審核"
                        ),
                    )

            policy = session.query(ApprovalPolicy).filter(
                ApprovalPolicy.submitter_role == item.submitter_role,
                ApprovalPolicy.doc_type == "all",
            ).first()

            if policy:
                policy.approver_roles = item.approver_roles
                policy.is_active = item.is_active
            else:
                session.add(ApprovalPolicy(
                    doc_type="all",
                    submitter_role=item.submitter_role,
                    approver_roles=item.approver_roles,
                    is_active=item.is_active,
                ))

        session.commit()
        logger.warning("審核政策已由 %s 更新", current_user.get("username"))
        return {"message": "審核政策已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/approval-settings/logs")
def get_approval_logs(
    doc_type: Optional[str] = Query(None, description="leave / overtime / punch_correction"),
    doc_id: Optional[int] = Query(None),
    current_user: dict = Depends(require_permission(Permission.LEAVES_READ)),
):
    """查詢簽核記錄（需 LEAVES_READ 或 OVERTIME_READ 權限）"""
    session = get_session()
    try:
        q = session.query(ApprovalLog)
        if doc_type:
            q = q.filter(ApprovalLog.doc_type == doc_type)
        if doc_id is not None:
            q = q.filter(ApprovalLog.doc_id == doc_id)
        logs = q.order_by(ApprovalLog.created_at.desc()).all()
        return [
            {
                "id": log.id,
                "doc_type": log.doc_type,
                "doc_id": log.doc_id,
                "action": log.action,
                "approver_id": log.approver_id,
                "approver_username": log.approver_username,
                "approver_role": log.approver_role,
                "comment": log.comment,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs
        ]
    finally:
        session.close()
