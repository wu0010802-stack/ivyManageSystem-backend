"""DB-driven 自訂權限/角色 admin CRUD 對應 Out schemas（Phase 3.5）。

涵蓋 6 個 grandfather endpoint（全部走 Permission.ROLES_MANAGE 守衛）：
- create_permission_definition / update_permission_definition → PermissionDefinitionOut
- delete_permission_definition → PermissionAdminOkOut（{ok: bool}）
- create_role / update_role → RoleOut
- delete_role → PermissionAdminOkOut（{ok: bool}）

角色 code/label/description 與 permission code 皆非 PII，不需 pii-allow。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class PermissionDefinitionOut(IvyBaseModel):
    """create_permission_definition / update_permission_definition 回傳。"""

    code: str
    label: str
    description: Optional[str] = None
    group_name: str
    is_core: bool


class RoleOut(IvyBaseModel):
    """create_role / update_role 回傳。"""

    code: str
    label: str
    description: Optional[str] = None
    permissions: list[str]
    is_core: bool


class PermissionAdminOkOut(IvyBaseModel):
    """delete_permission_definition / delete_role 回傳 — {ok: bool}。

    不重用 _common.OkStatusOut（欄位是 status:str）或 DeleteResultOut（message:str），
    重用會 silent rename 前端欄位。
    """

    ok: bool
