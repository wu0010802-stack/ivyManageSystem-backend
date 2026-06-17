"""DB-driven 自訂角色 admin CRUD（(b) 子專案）。

3 endpoint，全部走 Permission.ROLES_MANAGE 守衛：
- POST   /api/roles                          新增自訂角色
- PUT    /api/roles/{code}                    改 label/description/permissions
- DELETE /api/roles/{code}                    刪自訂角色
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from models.database import User, get_session_dep
from models.permission_models import Role
from schemas.permissions_admin import (
    PermissionAdminOkOut,
    RoleOut,
)
from utils.auth import require_permission
from utils.permissions import Permission, has_permission, validate_permission_names
from utils.portfolio_access import is_unrestricted

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["permissions-admin"])


def _assert_can_grant(current_user: dict, requested: list[str]) -> None:
    """RA-L2：caller 不得授出超過自身持有的權限（防經自訂角色自我提權）。

    wildcard caller 不受限；其餘 caller 要授的每個權限，其 base code 必須為 caller
    持有（含 scope grant）。授 '*' 一律僅限 wildcard caller。
    """
    caller_perms = current_user.get("permission_names") or []
    if "*" in caller_perms:
        return
    # R6-7：scope-aware（原本 code.split(":")[0] 剝 scope → own_class caller 可在角色
    # 塞 :all 提權；經下游 _assert_can_manage_user 字串子集擋故不可利用，但對齊收緊）。
    over = []
    for code in requested:
        if code == "*":
            over.append(code)  # 只有 wildcard caller 可授 *（已在上方 return）
            continue
        base, _, scope = code.partition(":")
        if scope in ("", "all"):
            # 授 bare / :all 需 caller 在 base 上 unrestricted（bare/:all/wildcard）
            ok = is_unrestricted(current_user, code=base)
        else:
            # 授 :own_class 等窄 scope 需 caller 至少持有 base（任一 scope）
            ok = has_permission(caller_perms, base)
        if not ok:
            over.append(code)
    if over:
        raise HTTPException(
            status_code=403, detail=f"不可授出超過自身持有的權限：{over}"
        )


# ============================================================
# Pydantic schemas
# ============================================================


class RoleIn(BaseModel):
    code: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    label: str = Field(..., min_length=1, max_length=40)
    description: Optional[str] = Field(None, max_length=200)
    permissions: List[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=40)
    description: Optional[str] = Field(None, max_length=200)
    permissions: Optional[List[str]] = None


# ============================================================
# Role CRUD
# ============================================================


@router.post("/roles", response_model=RoleOut)
def create_role(
    payload: RoleIn,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    existing = session.query(Role).filter_by(code=payload.code).first()
    if existing is not None:
        raise HTTPException(status_code=422, detail=f"角色 code 已存在：{payload.code}")

    # 驗證 permissions 合法：剝 scope 後綴後驗 base enum + scope 值（#3, 2026-06-17）。
    # 與 per-user 覆寫路徑（api/auth.py validate_permission_names）統一，使自訂角色可指派
    # row-level scope（如 STUDENTS_READ:own_class）；舊版以 DB code 原值存在性檢查會誤判 422。
    invalid = validate_permission_names(payload.permissions)
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"以下 permission code 不合法（不存在或 scope 不正確）：{invalid}",
        )

    _assert_can_grant(current_user, payload.permissions)

    role = Role(
        code=payload.code,
        label=payload.label,
        description=payload.description,
        permissions=list(payload.permissions),
        is_core=False,
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    return {
        "code": role.code,
        "label": role.label,
        "description": role.description,
        "permissions": list(role.permissions),
        "is_core": role.is_core,
    }


@router.put("/roles/{code}", response_model=RoleOut)
def update_role(
    code: str,
    payload: RoleUpdate,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    role = session.query(Role).filter_by(code=code).first()
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")

    if payload.permissions is not None:
        if role.is_core:
            raise HTTPException(
                status_code=409,
                detail="核心角色的權限不可修改（僅可改 label/description）",
            )
        invalid = validate_permission_names(payload.permissions)
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"以下 permission code 不合法（不存在或 scope 不正確）：{invalid}",
            )
        _assert_can_grant(current_user, payload.permissions)
        role.permissions = list(payload.permissions)

        # bump token_version for users 依此 role 預設（permission_names IS NULL）
        # 注意：SQLite 儲存 JSON null 為字串 'null'，SQL IS NULL 無法比對；
        # 改用 app 層 Python 過濾確保 SQLite/PG 皆相容。
        role_users = session.query(User).filter(User.role == code).all()
        affected = [u for u in role_users if u.permission_names is None]
        for u in affected:
            u.token_version = (u.token_version or 0) + 1

    if payload.label is not None:
        role.label = payload.label
    if payload.description is not None:
        role.description = payload.description

    session.commit()
    session.refresh(role)
    return {
        "code": role.code,
        "label": role.label,
        "description": role.description,
        "permissions": list(role.permissions),
        "is_core": role.is_core,
    }


@router.delete("/roles/{code}", response_model=PermissionAdminOkOut)
def delete_role(
    code: str,
    session: Session = Depends(get_session_dep),
    _: dict = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    role = session.query(Role).filter_by(code=code).first()
    if role is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    if role.is_core:
        raise HTTPException(status_code=409, detail="核心角色不可刪除")

    user_count = session.query(User).filter_by(role=code).count()
    if user_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"尚有 {user_count} 個帳號使用此角色，請先變更帳號角色再刪除",
        )

    session.delete(role)
    session.commit()
    return {"ok": True}
