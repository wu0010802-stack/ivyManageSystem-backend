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
from models.permission_models import PermissionDefinition, Role
from schemas.permissions_admin import (
    PermissionAdminOkOut,
    RoleOut,
)
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["permissions-admin"])


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
    _: dict = Depends(require_permission(Permission.ROLES_MANAGE)),
):
    existing = session.query(Role).filter_by(code=payload.code).first()
    if existing is not None:
        raise HTTPException(status_code=422, detail=f"角色 code 已存在：{payload.code}")

    # Validate permissions exist
    invalid = []
    for perm_code in payload.permissions:
        if perm_code == "*":
            continue
        if (
            not session.query(PermissionDefinition.code)
            .filter_by(code=perm_code)
            .first()
        ):
            invalid.append(perm_code)
    if invalid:
        raise HTTPException(
            status_code=422, detail=f"以下 permission code 不存在：{invalid}"
        )

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
    _: dict = Depends(require_permission(Permission.ROLES_MANAGE)),
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
        invalid = []
        for perm_code in payload.permissions:
            if perm_code == "*":
                continue
            if (
                not session.query(PermissionDefinition.code)
                .filter_by(code=perm_code)
                .first()
            ):
                invalid.append(perm_code)
        if invalid:
            raise HTTPException(
                status_code=422, detail=f"以下 permission code 不存在：{invalid}"
            )
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
