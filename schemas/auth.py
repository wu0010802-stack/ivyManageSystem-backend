"""教師/管理員 auth router 對應 Out schemas（Phase 3.5）。

涵蓋 9 個 grandfather endpoint：
- impersonate_user / login / refresh_token / end_impersonate → 帶 user payload + access_token cookie
- get_me → 直接回 user dict
- logout / change_password → message-only response
- list_users → admin 列表
- get_permissions → DB-driven 權限定義字典（動態 key，用 dict[str, Any]）

FastAPI 對 JSONResponse return 不做 response_model validation；本檔的 schema 主要
用於 OpenAPI codegen（前端拿正確 TS 型別）。
"""

from __future__ import annotations

from typing import Any, Optional

from schemas._base import IvyBaseModel


class AuthUserOut(IvyBaseModel):
    """登入態使用者基本資訊（login / refresh / impersonate / end_impersonate / get_me 共用）。"""

    id: int
    username: str  # pii-allow: 員工帳號（admin/self 可見）
    role: str
    role_label: str
    permission_names: list[str]
    employee_id: Optional[int] = None
    name: str  # pii-allow: 員工姓名（自身 + admin 可見）
    title: Optional[str] = None
    impersonation_mode: Optional[str] = None  # 模擬中才有：'readonly' / 'write'；非模擬為 None


class AuthLoginResultOut(IvyBaseModel):
    """POST /login 成功回傳：使用者 + must_change_password 旗標 + cookie。"""

    must_change_password: bool  # pii-allow: 強制改密旗標非實際密碼值
    user: AuthUserOut


class AuthUserResultOut(IvyBaseModel):
    """impersonate / refresh / end_impersonate 共用：{user: AuthUserOut}。"""

    user: AuthUserOut


class AuthMessageOut(IvyBaseModel):
    """logout / change_password message-only response。"""

    message: str


class AuthAdminUserItemOut(IvyBaseModel):
    """GET /users 後台使用者列表單筆。"""

    id: int
    username: str  # pii-allow: 員工帳號（USER_MANAGEMENT_READ 必看）
    role: str
    role_label: str
    permission_names: list[str]
    is_active: bool
    employee_id: Optional[int] = None
    employee_name: str  # pii-allow: 員工姓名（USER_MANAGEMENT_READ 必看）
    last_login: Optional[str] = None


# get_permissions 動態 shape（permissions: dict[code, ...] + groups: list[dict] + roles: dict[code, ...]），
# 嚴格 schema 會犧牲 admin runtime 改動的彈性；top-level 用 dict[str, Any] / list[dict].
class AuthPermissionsDefinitionOut(IvyBaseModel):
    """GET /permissions 權限定義（DB-driven，admin runtime 改動立即生效）。"""

    permissions: dict[str, Any]
    groups: list[dict[str, Any]]  # 含 {name, permissions, split_permissions} 動態
    roles: dict[str, Any]  # role_code → {label, description, permissions, is_core}


class RevokeSessionOut(IvyBaseModel):
    """DELETE /sessions/{family_id} — Per-session revoke."""

    revoked: int


class LogoutAllSessionsOut(IvyBaseModel):
    """POST /sessions/logout-all — Logout all + bump token_version."""

    logout_all: bool
