"""
Authentication & user management router
"""

import ipaddress
import logging
import time
from collections import defaultdict
from datetime import datetime
from hashlib import sha256
from utils.taipei_time import now_taipei_naive
from typing import List, Literal, Optional

from sqlalchemy import text

from config import settings

from fastapi import APIRouter, Depends, HTTPException, Request
from utils.errors import raise_safe_500
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from models.database import get_session, User, Employee
from models.staff_refresh_token import StaffRefreshToken
from schemas._common import DeleteResultOut, MutationResultOut
from schemas.auth import (
    AuthAdminUserItemOut,
    AuthLoginResultOut,
    AuthMessageOut,
    AuthPermissionsDefinitionOut,
    AuthUserOut,
    AuthUserResultOut,
    LogoutAllSessionsOut,
    RevokeSessionOut,
)
from utils.audit import write_login_audit, mark_soft_delete, write_audit_in_session
from utils.request_ip import get_client_ip
from utils.error_messages import USER_NOT_FOUND, EMPLOYEE_DOES_NOT_EXIST
from utils.auth import (
    hash_password,
    verify_password,
    needs_rehash,
    create_access_token,
    get_current_user,
    decode_token_allow_expired,
    require_staff_permission,
    validate_password_strength,
)
from utils.password_history import (
    assert_not_recently_used,
    record as record_password_history,
)
from utils.cookie import (
    set_access_token_cookie,
    clear_access_token_cookie,
    set_admin_token_cookie,
    clear_admin_token_cookie,
    set_staff_refresh_cookie,
    clear_staff_refresh_cookie,
)
from utils.permissions import Permission
from utils.permissions import (
    get_permissions_definition,
    get_role_default_permissions,
    has_permission,
    permissions_subset,
    resolve_user_permissions,
    validate_permission_names,
    ROLE_LABELS,
    ROLE_TEMPLATES,
    WILDCARD,
)

# 已知核心角色白名單（對齊 ROLE_TEMPLATES / DB roles 表 seed 的 7 個 is_core 角色）。
# create/update user 的 role 欄位只接受此集合，未知字串一律 422——避免寫入任意角色
# 字串繞過以角色字串為準的安全閘（如 has_permission 對 role=='teacher' 短路、
# _assert_can_manage_user 對 'admin' 的判定）。bh-misc #29。
KNOWN_ROLE_CODES = frozenset(ROLE_TEMPLATES.keys())


def _validate_role_code(value: Optional[str]) -> Optional[str]:
    """共用 role 白名單驗證：None 放行（不改角色）；未知值 raise → Pydantic 422。"""
    if value is None:
        return value
    if value not in KNOWN_ROLE_CODES:
        raise ValueError(f"未知角色 '{value}'，僅允許：{sorted(KNOWN_ROLE_CODES)}")
    return value


logger = logging.getLogger(__name__)


def _get_school_wifi_networks() -> list:
    raw_list = settings.network.school_wifi_ips
    if not raw_list:
        return []
    result = []
    for entry in raw_list:
        entry = entry.strip()
        if entry:
            try:
                result.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                logger.warning("SCHOOL_WIFI_IPS 無效項目: %s", entry)
    return result


def _is_school_wifi(ip_str: str) -> bool:
    networks = _get_school_wifi_networks()
    if not networks:
        return True  # 未設定白名單 → 全部放行
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in networks)
    except ValueError:
        return False


def warn_if_school_wifi_gate_disabled(is_production: bool) -> None:
    """正式環境未設 SCHOOL_WIFI_IPS → 教師登入的學校 WiFi 閘形同停用（fail-open），
    啟動時告警以避免 silent 失效（任何取得教師帳密者可從任意公網 IP 登入）。

    不採 fail-fast raise（與 CORS 不同：WiFi 閘可被合法關閉，例如教師遠端辦公）；
    僅 prod 下告警。若業主要硬性強制此閘，將 logger.warning 改為 raise 即可。
    """
    if is_production and not _get_school_wifi_networks():
        logger.warning(
            "SCHOOL_WIFI_IPS 未設定：正式環境下教師登入的學校 WiFi 閘形同停用，"
            "任何取得教師帳密者可從任意公網 IP 登入。如需此閘請設定 SCHOOL_WIFI_IPS（CIDR 清單）。"
        )


def _assert_can_manage_user(
    current_user: dict,
    *,
    session=None,
    target_user: Optional[User] = None,
    payload_role: Optional[str] = None,
    payload_permission_names: Optional[List[str]] = None,
) -> None:
    """USER_MANAGEMENT_WRITE 守衛：caller 不可超過自身權限管理他人。

    1. caller_role == 'admin' → 一律放行
    2. target_user.role == 'admin' → 拒絕（不可動 admin 帳號）
    3. payload_role == 'admin' → 拒絕（不可指定 admin 角色）
    4. 「最終權限」(payload_permission_names or 角色預設) 必須 ⊆ caller permission_names
    """
    caller_role = current_user.get("role")
    if caller_role == "admin":
        return

    caller_id = current_user.get("user_id")
    caller_perms = current_user.get("permission_names") or []
    caller_has_wildcard = WILDCARD in caller_perms
    caller_set = set(caller_perms)

    if target_user is not None and target_user.role == "admin":
        logger.warning(
            "user-management 拒絕：caller user_id=%s role=%s 嘗試管理 admin user_id=%s",
            caller_id,
            caller_role,
            target_user.id,
        )
        raise HTTPException(status_code=403, detail="不可管理管理員帳號")

    if payload_role == "admin":
        logger.warning(
            "user-management 拒絕：caller user_id=%s role=%s 嘗試指定 admin 角色",
            caller_id,
            caller_role,
        )
        raise HTTPException(status_code=403, detail="不可指定 admin 角色")

    # target 的現有權限必須是 caller 的子集；防止 caller 透過 reset_password /
    # delete / is_active 等不帶 permissions 的操作，間接管理其實沒權限管的對象後
    # 透過接管密碼登入該帳號取得超額權限（提權鏈）。
    if target_user is not None and target_user.id != caller_id:
        target_perms = resolve_user_permissions(target_user, session)
        target_set = set(target_perms)
        # caller 有 wildcard → 任何 target 都可管；否則檢查 target 是否 ⊆ caller
        # （target 也有 wildcard 但 caller 沒 → reject，因為 "*" ∉ caller_set）
        if not caller_has_wildcard:
            extra = target_set - caller_set
            if extra:
                logger.warning(
                    "user-management 拒絕：caller user_id=%s 權限 %s 不足以管理 target user_id=%s 權限 %s（多: %s）",
                    caller_id,
                    sorted(caller_set),
                    target_user.id,
                    sorted(target_set),
                    sorted(extra),
                )
                raise HTTPException(
                    status_code=403, detail="目標帳號的權限超出您的管理範圍"
                )

    final_perms = payload_permission_names
    if final_perms is None and payload_role is not None:
        if session is None:
            raise RuntimeError(
                "_assert_can_manage_user 需要 session 才能解析 role 預設權限"
            )
        final_perms = get_role_default_permissions(session, payload_role)

    if final_perms is not None:
        final_set = set(final_perms)
        if not caller_has_wildcard:
            extra = final_set - caller_set
            if extra:
                logger.warning(
                    "user-management 拒絕：caller user_id=%s 權限 %s 不足以授予 %s（多: %s）",
                    caller_id,
                    sorted(caller_set),
                    sorted(final_set),
                    sorted(extra),
                )
                raise HTTPException(
                    status_code=403, detail="授予的權限超出您本身擁有的範圍"
                )


router = APIRouter(prefix="/api/auth", tags=["auth"])

# 預計算的假密碼雜湊，用於帳號不存在時仍執行 PBKDF2 運算
# 使回應時間與「帳號存在但密碼錯誤」一致，防止 Timing Side-Channel 枚舉帳號
_DUMMY_PASSWORD_HASH = hash_password("__dummy_timing_padding__")

# ---------- Login Rate Limiter（雙層防護）----------
#
# 層級一：IP 滑動視窗
#   同一 IP 在 _IP_WINDOW 秒內最多 _IP_MAX_ATTEMPTS 次登入嘗試（不分成敗）
#   防止：大量帳號爆破（Credential Stuffing）、分散式暴力破解
#
# 層級二：帳號失敗鎖定
#   同一帳號連續失敗 _FAIL_THRESHOLD 次，鎖定 _FAIL_LOCKOUT 秒
#   登入成功後自動解除（reset 失敗計數）
#   防止：針對特定帳號的定向暴力破解

_IP_WINDOW = 300  # IP 滑動視窗長度：5 分鐘
_IP_MAX_ATTEMPTS = 20  # 同 IP 視窗內最多嘗試次數
_FAIL_THRESHOLD = 5  # 帳號連續失敗次數上限
_FAIL_LOCKOUT = 900  # 帳號鎖定時間：15 分鐘

# DB-backed counter scopes（rate_limit_buckets.bucket_key prefix）
_IP_SCOPE = "login_ip"
_ACCOUNT_SCOPE = "login_account"

# Password endpoint scopes（與 login_* scope 隔離，避免互相干擾）
_PWD_CHANGE_IP_SCOPE = "pwd_change_ip"
_PWD_CHANGE_USER_SCOPE = "pwd_change_user"
_PWD_RESET_IP_SCOPE = "pwd_reset_ip"
# window/threshold 復用既有 _IP_WINDOW / _IP_MAX_ATTEMPTS / _FAIL_THRESHOLD / _FAIL_LOCKOUT

# In-process dict 仍保留，作為「DB 失敗時的 fail-open 配套」與測試 fixture
# reset target；正式擋線靠 DB-backed counter（multi-worker 安全）。
# Refs: 邏輯漏洞 audit 2026-05-07 P0 #14（user 拍板採 DB-backed 方案）。
_ip_attempts: dict[str, list[float]] = defaultdict(list)
_account_failures: dict[str, list[float]] = defaultdict(list)


def _check_ip_rate_limit(ip: str) -> None:
    """IP 層級滑動視窗限流：超出則拋 429。

    走 DB-backed counter（rate_limit_buckets 表），多 worker 一致。
    DB 失敗時 fail-open（utils/rate_limit_db.py 各 helper 內部 log 警告）。
    """
    from utils.rate_limit_db import count_recent_attempts, record_attempt

    record_attempt(_IP_SCOPE, ip, window_seconds=_IP_WINDOW)
    count = count_recent_attempts(
        _IP_SCOPE, ip, within_seconds=_IP_WINDOW, fail_closed=True
    )
    if count > _IP_MAX_ATTEMPTS:
        logger.warning("IP 登入頻率超限: %s (count=%d)", ip, count)
        raise HTTPException(status_code=429, detail="登入嘗試次數過多，請稍後再試")


def _check_account_lockout(username: str) -> None:
    """帳號層級失敗鎖定：累積失敗 _FAIL_THRESHOLD 次後拋 429。

    走 DB-backed counter；多 worker 一致。失敗時 fail-open。
    """
    from utils.rate_limit_db import count_recent_attempts

    count = count_recent_attempts(
        _ACCOUNT_SCOPE, username, within_seconds=_FAIL_LOCKOUT, fail_closed=True
    )
    if count >= _FAIL_THRESHOLD:
        logger.warning("帳號已鎖定: %s (failures=%d)", username, count)
        raise HTTPException(
            status_code=429,
            detail="密碼錯誤次數過多，帳號已暫時鎖定，請稍後再試",
        )


def _record_login_failure(username: str) -> None:
    """記錄帳號登入失敗一次（DB-backed bucket）。"""
    from utils.rate_limit_db import record_attempt

    record_attempt(_ACCOUNT_SCOPE, username, window_seconds=_FAIL_LOCKOUT)


def _clear_login_failures(username: str) -> None:
    """登入成功後清除帳號的失敗記錄（DB-backed bucket）。"""
    from utils.rate_limit_db import clear_attempts

    clear_attempts(_ACCOUNT_SCOPE, username)


def _check_pwd_change_ip(ip: str) -> None:
    """change-password per-IP 滑動視窗（不分成敗都計數）。

    走 DB-backed counter；多 worker 一致。DB 失敗時 fail-open
    （utils/rate_limit_db.py 各 helper 內部 log 警告）。

    與 login flow `_check_ip_rate_limit` 結構幾乎一樣但獨立 scope —
    spec §3.3 deliberate（不抽 generic helper，避免重構 login flow）。
    """
    from utils.rate_limit_db import count_recent_attempts, record_attempt

    record_attempt(_PWD_CHANGE_IP_SCOPE, ip, window_seconds=_IP_WINDOW)
    count = count_recent_attempts(
        _PWD_CHANGE_IP_SCOPE, ip, within_seconds=_IP_WINDOW, fail_closed=True
    )
    if count > _IP_MAX_ATTEMPTS:
        logger.warning("change-password IP 頻率超限: %s (count=%d)", ip, count)
        raise HTTPException(status_code=429, detail="請求過於頻繁，請稍後再試")


def _check_pwd_change_user_lockout(user_id: int) -> None:
    """change-password per-user_id 失敗鎖定（僅在 verify_password 失敗時遞增）。

    走 DB-backed counter；多 worker 一致。DB 失敗時 fail-open。
    與 login flow `_check_account_lockout` 獨立 scope（spec §3.3 deliberate）。
    """
    from utils.rate_limit_db import count_recent_attempts

    key = f"user:{user_id}"
    count = count_recent_attempts(
        _PWD_CHANGE_USER_SCOPE, key, within_seconds=_FAIL_LOCKOUT, fail_closed=True
    )
    if count >= _FAIL_THRESHOLD:
        logger.warning(
            "change-password 失敗次數超限: user_id=%d (failures=%d)", user_id, count
        )
        raise HTTPException(
            status_code=429,
            detail="密碼修改失敗次數過多，請稍後再試",
        )


def _record_pwd_change_failure(user_id: int) -> None:
    """記錄 change-password 失敗一次（DB-backed bucket）。"""
    from utils.rate_limit_db import record_attempt

    record_attempt(
        _PWD_CHANGE_USER_SCOPE, f"user:{user_id}", window_seconds=_FAIL_LOCKOUT
    )


def _clear_pwd_change_failures(user_id: int) -> None:
    """change-password 成功後清除失敗記錄。"""
    from utils.rate_limit_db import clear_attempts

    clear_attempts(_PWD_CHANGE_USER_SCOPE, f"user:{user_id}")


def _check_pwd_reset_ip(ip: str) -> None:
    """reset-password per-caller IP 滑動視窗（防 admin cookie 被竊狂刷別人）。

    走 DB-backed counter；多 worker 一致。DB 失敗時 fail-open。
    與 login flow `_check_ip_rate_limit` 獨立 scope（spec §3.3 deliberate
    + §3.4 「不對 target user 套 lockout」設計理由）。
    """
    from utils.rate_limit_db import count_recent_attempts, record_attempt

    record_attempt(_PWD_RESET_IP_SCOPE, ip, window_seconds=_IP_WINDOW)
    count = count_recent_attempts(
        _PWD_RESET_IP_SCOPE, ip, within_seconds=_IP_WINDOW, fail_closed=True
    )
    if count > _IP_MAX_ATTEMPTS:
        logger.warning("reset-password IP 頻率超限: %s (count=%d)", ip, count)
        raise HTTPException(status_code=429, detail="請求過於頻繁，請稍後再試")


# ============ Pydantic Models ============


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    employee_id: Optional[int] = None  # None = 純管理帳號，不關聯員工記錄
    username: str
    password: str
    role: str = "teacher"
    permission_names: Optional[List[str]] = None  # None 表示使用角色預設權限

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        # 未知角色字串一律 422（白名單對齊 KNOWN_ROLE_CODES）。bh-misc #29。
        return _validate_role_code(v)


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    permission_names: Optional[List[str]] = None
    is_active: Optional[bool] = None

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: Optional[str]) -> Optional[str]:
        # None=不改角色放行；其餘須在白名單內，否則 422。bh-misc #29。
        return _validate_role_code(v)


class ResetPasswordRequest(BaseModel):
    new_password: str


class ImpersonateRequest(BaseModel):
    employee_id: int
    mode: Literal["readonly", "write"] = "readonly"


class SessionItemOut(BaseModel):
    family_id: str
    last_active: datetime
    user_agent: str | None
    ip: str | None
    token_count: int
    is_current: bool = False  # 標記當前裝置的 session


# ============ Public Routes ============


@router.post("/impersonate", response_model=AuthUserResultOut)
def impersonate_user(
    data: ImpersonateRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """切換使用者身份（管理員或園長限定，依 mode 分流）"""
    # 防巢狀模擬：已在模擬中（access_token 帶 impersonated_by）要求先退出。
    # 置於權限閘之前，回 409 比 403 語意清楚（避免用被模擬老師的權限判斷）。
    if current_user.get("impersonated_by") is not None:
        raise HTTPException(status_code=409, detail="請先退出目前模擬再切換")

    user_perms = current_user.get("permission_names")
    required = (
        Permission.PORTAL_IMPERSONATE
        if data.mode == "write"
        else Permission.PORTAL_PREVIEW
    )
    if not has_permission(user_perms, required):
        raise HTTPException(status_code=403, detail="您沒有此功能的存取權限")

    session = get_session()
    try:
        # 1. 檢查目標員工是否存在
        target_emp = (
            session.query(Employee).filter(Employee.id == data.employee_id).first()
        )
        if not target_emp:
            raise HTTPException(status_code=404, detail=EMPLOYEE_DOES_NOT_EXIST)

        # 2. 尋找該員工的使用者帳號
        target_user = (
            session.query(User).filter(User.employee_id == data.employee_id).first()
        )

        # 3. 如果沒有使用者帳號，拒絕切換
        if not target_user:
            raise HTTPException(
                status_code=400, detail="該員工沒有使用者帳號，無法切換"
            )

        # 4. 禁止冒充 admin（防止平級或提權冒充）
        if target_user.role == "admin":
            logger.warning(
                "冒充被拒（目標為 admin）：操作者 user_id=%s 嘗試冒充 user_id=%s",
                current_user.get("user_id"),
                target_user.id,
            )
            raise HTTPException(status_code=403, detail="不可冒充管理員帳號")

        # 4.5 禁止冒充已停用帳號（停用後應立即失效，不因冒充繞過）
        if not target_user.is_active:
            logger.warning(
                "冒充被拒（帳號已停用）：操作者 user_id=%s 嘗試冒充已停用 user_id=%s",
                current_user.get("user_id"),
                target_user.id,
            )
            raise HTTPException(status_code=403, detail="無法冒充已停用的帳號")

        # NV7：禁止冒充已離職員工（員工軟刪除後即使帳號未停用亦不可被模擬）
        if not target_emp.is_active:
            logger.warning(
                "冒充被拒（員工已離職）：操作者 user_id=%s 嘗試冒充已離職 employee_id=%s",
                current_user.get("user_id"),
                target_emp.id,
            )
            raise HTTPException(status_code=403, detail="無法冒充已離職員工")

        # 5. 產生該使用者的 token
        permission_names = resolve_user_permissions(target_user, session)

        # 4.6（C13）越權預覽防護：目標權限集必須 ⊆ 操作者權限集（含 scope 維度）。
        # 否則 principal（PORTAL_PREVIEW 但無 EMPLOYEES_READ）可藉冒充 HR 讀全園
        # 員工個資。admin（wildcard）為任何人之 superset 仍可；冒充權限 ⊆ 自己的
        # 一般 teacher 仍可。
        if not permissions_subset(permission_names, user_perms):
            logger.warning(
                "冒充被拒（目標權限超出操作者）：操作者 user_id=%s 嘗試冒充 user_id=%s",
                current_user.get("user_id"),
                target_user.id,
            )
            raise HTTPException(status_code=403, detail="不可冒充權限高於您的帳號")

        target_token = create_access_token(
            {
                "user_id": target_user.id,
                "employee_id": target_user.employee_id,
                "role": target_user.role,
                "name": target_emp.name,
                "permission_names": permission_names,
                "token_version": target_user.token_version,
                "impersonated_by": current_user.get("user_id"),
                "impersonated_by_name": current_user.get("name"),
                # qa-loop #4：帶入 admin 本人 token_version，使 _resolve_user_auth_fields
                # 能在模擬期間驗 admin 憑證有效性（reset_password/改權/logout-all 後即時失效）。
                "impersonator_token_version": current_user.get("token_version", 0),
                "impersonation_mode": data.mode,
            }
        )

        # 6. 取得管理員原始 token（用於備份到 admin_token Cookie）
        admin_token = request.cookies.get("admin_token") or request.cookies.get(
            "access_token"
        )
        if not admin_token:
            authorization = request.headers.get("authorization", "")
            if authorization.startswith("Bearer "):
                admin_token = authorization.split(" ", 1)[1]

        # 7. 寫入審計日誌（明確標記操作者與被冒充對象，供事後追查）
        client_ip = get_client_ip(request) or "unknown"
        logger.warning(
            "冒充操作：操作者 user_id=%s 切換為 user_id=%s（role=%s）來源 IP=%s",
            current_user.get("user_id"),
            target_user.id,
            target_user.role,
            client_ip,
        )
        _mode_label = "代操作" if data.mode == "write" else "預覽"
        request.state.audit_summary = (
            f"[{_mode_label}] 操作者 {current_user.get('name')}"
            f"（user_id={current_user.get('user_id')}）"
            f" {'切換為' if data.mode == 'write' else '檢視'} {target_emp.name}"
            f"（user_id={target_user.id}）"
        )

        response = JSONResponse(
            content={
                "user": {
                    "id": target_user.id,
                    "username": target_user.username,
                    "role": target_user.role,
                    "role_label": ROLE_LABELS.get(target_user.role, target_user.role),
                    "permission_names": permission_names,
                    "employee_id": target_user.employee_id,
                    "name": target_emp.name,
                    "title": (
                        target_emp.job_title_rel.name
                        if target_emp.job_title_rel
                        else (target_emp.title or "")
                    ),
                    "impersonation_mode": data.mode,
                },
            }
        )
        set_access_token_cookie(response, target_token)
        # 備份管理員 Token 到 admin_token Cookie（httpOnly，前端無法讀取）
        if admin_token:
            set_admin_token_cookie(response, admin_token)
        return response
    finally:
        session.close()


@router.post("/login", response_model=AuthLoginResultOut)
def login(data: LoginRequest, request: Request):
    """教師/管理員登入"""
    client_ip = get_client_ip(request) or "unknown"
    # 層級一：IP 滑動視窗（不分成敗，防 Credential Stuffing）
    try:
        _check_ip_rate_limit(client_ip)
    except HTTPException:
        write_login_audit(
            request,
            action="LOGIN_RATE_LIMITED",
            username=data.username,
            extras={"ip": client_ip, "scope": "ip_sliding_window"},
        )
        raise
    # 層級二：帳號失敗鎖定（只在密碼錯誤時遞增，防定向暴力破解）
    try:
        _check_account_lockout(data.username)
    except HTTPException:
        write_login_audit(
            request,
            action="LOGIN_LOCKED",
            username=data.username,
            extras={"scope": "account_lockout"},
        )
        raise

    session = get_session()
    try:
        user = (
            session.query(User)
            .filter(
                User.username == data.username,
                User.is_active == True,
            )
            .first()
        )

        if not user:
            # 帳號不存在：仍執行密碼驗證（對假 hash），使回應時間與「密碼錯誤」一致
            # 防止攻擊者透過回應時間差異枚舉有效帳號（Timing Side-Channel）
            verify_password(data.password, _DUMMY_PASSWORD_HASH)
            _record_login_failure(data.username)
            write_login_audit(
                request,
                action="LOGIN_FAILED",
                username=data.username,
                extras={"reason": "wrong_credentials"},
            )
            raise HTTPException(status_code=401, detail="帳號或密碼錯誤")

        if not verify_password(data.password, user.password_hash):
            _record_login_failure(data.username)  # 記錄失敗，累積後觸發鎖定
            write_login_audit(
                request,
                action="LOGIN_FAILED",
                username=data.username,
                extras={"reason": "wrong_credentials"},
            )
            raise HTTPException(status_code=401, detail="帳號或密碼錯誤")

        # 登入成功：清除帳號失敗記錄
        _clear_login_failures(data.username)

        # 教師角色須從學校 WiFi 登入
        if user.role == "teacher" and not _is_school_wifi(client_ip):
            write_login_audit(
                request,
                action="LOGIN_FAILED",
                username=data.username,
                extras={"reason": "non_school_wifi", "ip": client_ip},
            )
            raise HTTPException(status_code=403, detail="請連接學校 WiFi 後再登入")

        # 透明升級：若密碼是舊格式（100,000 次迭代），趁登入時無感升級至 600,000 次
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(data.password)
            logger.info("使用者 %s 密碼雜湊已自動升級至新迭代次數", user.username)

        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()

        user.last_login = now_taipei_naive()
        session.commit()

        # permission_names: ["*"] 表示全部權限；None 時套用角色預設
        permission_names = resolve_user_permissions(user, session)

        token = create_access_token(
            {
                "user_id": user.id,
                "employee_id": user.employee_id,
                "role": user.role,
                "name": emp.name if emp else "",
                "permission_names": permission_names,
                "token_version": user.token_version,
            }
        )

        write_login_audit(
            request,
            action="LOGIN_SUCCESS",
            username=data.username,
            user_id=user.id,
            extras={"role": user.role},
        )

        response = JSONResponse(
            content={
                "must_change_password": bool(user.must_change_password),
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "role": user.role,
                    "role_label": ROLE_LABELS.get(user.role, user.role),
                    "permission_names": permission_names,
                    "employee_id": user.employee_id,
                    "name": emp.name if emp else "",
                    "title": (
                        emp.job_title_rel.name
                        if emp and emp.job_title_rel
                        else (emp.title if emp else "")
                    ),
                },
            }
        )
        set_access_token_cookie(response, token)
        # Spec F: 簽 refresh token + 寫 staff_refresh_tokens + 記 UA/IP
        from services.staff_refresh import issue_refresh_token as _issue_staff_refresh

        _ua = request.headers.get("user-agent") or ""
        _refresh_raw, _ = _issue_staff_refresh(
            user_id=user.id,
            user_agent=_ua,
            ip=client_ip,
        )
        set_staff_refresh_cookie(response, _refresh_raw)
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise_safe_500(e)
    finally:
        session.close()


# ============ Token Refresh ============


@router.post("/refresh", response_model=AuthUserResultOut)
def refresh_token(request: Request):
    """以現有 token（可為剛過期）換發新 token。
    寬限期內的過期 token 仍可刷新，超過則需重新登入。
    Token 來源：httpOnly Cookie 或 Authorization header。

    Spec F 整合：若 staff_refresh_token cookie 存在，走 rotation 路徑（新 access +
    新 refresh cookie）；否則 fallback 既有 JWT grace period 路徑（向下相容）。
    """
    # 與 login 對稱：IP 滑動視窗限流，避免拿無效 token 壓 DB / 暴力試 jti。
    client_ip = get_client_ip(request) or "unknown"
    try:
        _check_ip_rate_limit(client_ip)
    except HTTPException:
        write_login_audit(
            request,
            action="TOKEN_REFRESH_FAILED",
            username=None,
            user_id=None,
            extras={"reason": "ip_rate_limited", "ip": client_ip},
        )
        raise

    # 模擬 session 不可經任何 refresh 路徑升級或洗掉歸屬（涵蓋 staff rotation 與 fallback）。
    _imp_tok = request.cookies.get("access_token")
    if not _imp_tok:
        _auth_h = request.headers.get("authorization", "")
        if _auth_h.startswith("Bearer "):
            _imp_tok = _auth_h.split(" ", 1)[1]
    if _imp_tok:
        from utils.auth import decode_token_for_audit

        _imp_payload = decode_token_for_audit(_imp_tok) or {}
        if _imp_payload.get("impersonated_by") is not None:
            write_login_audit(
                request,
                action="TOKEN_REFRESH_FAILED",
                username=_imp_payload.get("name"),
                user_id=_imp_payload.get("user_id"),
                extras={"reason": "impersonation_token_not_refreshable"},
            )
            raise HTTPException(
                status_code=401, detail="模擬工作階段不可刷新，請重新進入模擬"
            )

    # Spec F: staff_refresh_token cookie → rotation 路徑
    staff_refresh_raw = request.cookies.get("staff_refresh_token")
    if staff_refresh_raw:
        from services.staff_refresh import rotate_refresh_token as _rotate_staff

        _ua = request.headers.get("user-agent") or ""
        new_refresh_raw, rotated_user_id = _rotate_staff(
            staff_refresh_raw, _ua, client_ip
        )
        _session = get_session()
        try:
            _user = (
                _session.query(User)
                .filter(
                    User.id == rotated_user_id, User.is_active == True
                )  # noqa: E712
                .first()
            )
            if _user is None:
                raise HTTPException(status_code=401, detail="使用者已停用")
            # 鏡像下方 access-token fallback 路徑（line ~862）：must_change_password
            # 期間禁止 refresh，避免 staff rotation 路徑繞過強制改密碼守衛
            # （staff 路徑 c808d7f 落地時漏了 fallback 已有的此檢查）。
            if _user.must_change_password:
                write_login_audit(
                    request,
                    action="TOKEN_REFRESH_FAILED",
                    username=_user.username,
                    user_id=_user.id,
                    extras={"reason": "must_change_password"},
                )
                raise HTTPException(
                    status_code=403, detail="需先修改密碼後才能使用系統"
                )
            _emp = (
                _session.query(Employee)
                .filter(Employee.id == _user.employee_id)
                .first()
                if _user.employee_id
                else None
            )
            _perm = resolve_user_permissions(_user, _session)
            _new_access = create_access_token(
                {
                    "user_id": _user.id,
                    "employee_id": _user.employee_id,
                    "role": _user.role,
                    "name": _emp.name if _emp else "",
                    "permission_names": _perm,
                    "token_version": _user.token_version,
                }
            )
            _username_for_audit = _user.username
        finally:
            _session.close()

        write_login_audit(
            request,
            action="TOKEN_REFRESH",  # 對齊既有 audit action（test_audit_login.py 用此名）
            username=_username_for_audit,  # 對齊既存 test 期望 (帶實際 username 非 None)
            user_id=rotated_user_id,
        )
        _resp = JSONResponse(content={"message": "refreshed"})
        set_access_token_cookie(_resp, _new_access)
        set_staff_refresh_cookie(_resp, new_refresh_raw)
        return _resp

    # 從 Cookie 或 header 取得舊 token
    token = request.cookies.get("access_token")
    if not token:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1]
    if not token:
        write_login_audit(
            request,
            action="TOKEN_REFRESH_FAILED",
            username=None,
            user_id=None,
            extras={"reason": "no_token"},
        )
        raise HTTPException(status_code=401, detail="未提供認證 Token")

    # 允許過期的 token 解碼（在寬限期內）
    try:
        payload = decode_token_allow_expired(token)
    except HTTPException:
        write_login_audit(
            request,
            action="TOKEN_REFRESH_FAILED",
            username=None,
            user_id=None,
            extras={"reason": "invalid_token"},
        )
        raise

    # S2: absolute session lifetime — 從 original_iat 起算超過設定小時數即拒絕 refresh。
    # 缺欄位（舊 token 過渡）時不擋，待此次 refresh 後新 token 會帶上 original_iat。
    original_iat = payload.get("original_iat")
    if original_iat:
        from utils.auth import JWT_ABSOLUTE_LIFETIME_HOURS

        session_age_hours = (time.time() - int(original_iat)) / 3600
        if session_age_hours > JWT_ABSOLUTE_LIFETIME_HOURS:
            write_login_audit(
                request,
                action="TOKEN_REFRESH_FAILED",
                username=payload.get("name"),
                user_id=payload.get("user_id"),
                extras={
                    "reason": "absolute_lifetime_exceeded",
                    "session_age_hours": round(session_age_hours, 2),
                    "limit_hours": JWT_ABSOLUTE_LIFETIME_HOURS,
                },
            )
            raise HTTPException(
                status_code=401,
                detail=(
                    f"登入工作階段已超過 {JWT_ABSOLUTE_LIFETIME_HOURS} 小時上限，"
                    "請重新登入"
                ),
            )

    user_id = payload.get("user_id")
    if not user_id:
        write_login_audit(
            request,
            action="TOKEN_REFRESH_FAILED",
            username=payload.get("name"),
            user_id=None,
            extras={"reason": "incomplete_payload"},
        )
        raise HTTPException(status_code=401, detail="Token 資料不完整")

    # 驗證使用者仍然有效
    session = get_session()
    try:
        user = (
            session.query(User)
            .filter(User.id == user_id, User.is_active == True)
            .first()
        )
        if not user:
            write_login_audit(
                request,
                action="TOKEN_REFRESH_FAILED",
                username=payload.get("name"),
                user_id=user_id,
                extras={"reason": "user_inactive"},
            )
            raise HTTPException(status_code=401, detail="使用者已停用或不存在")
        if user.must_change_password:
            write_login_audit(
                request,
                action="TOKEN_REFRESH_FAILED",
                username=user.username,
                user_id=user.id,
                extras={"reason": "must_change_password"},
            )
            raise HTTPException(status_code=403, detail="需先修改密碼後才能使用系統")

        # 驗證 token_version：帳號停用或權限變更時版本遞增，使舊 token 無法換發
        # payload 缺少 token_version（舊 token 向下相容）時視為 0，與 DB 預設值相符
        if payload.get("token_version", 0) != user.token_version:
            write_login_audit(
                request,
                action="TOKEN_REFRESH_FAILED",
                username=user.username,
                user_id=user.id,
                extras={"reason": "token_version_mismatch"},
            )
            raise HTTPException(
                status_code=401, detail="Token 已失效，請重新登入（帳號狀態已變更）"
            )

        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()
        permission_names = resolve_user_permissions(user, session)

        # S2: 把舊 token 的 original_iat 帶進新 token，讓 absolute lifetime
        # 從首次登入算起，而非從本次 refresh 算起。
        new_token_payload = {
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": emp.name if emp else "",
            "permission_names": permission_names,
            "token_version": user.token_version,
        }
        if original_iat:
            new_token_payload["original_iat"] = int(original_iat)
        new_token = create_access_token(new_token_payload)

        write_login_audit(
            request,
            action="TOKEN_REFRESH",
            username=user.username,
            user_id=user.id,
        )

        response = JSONResponse(
            content={
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "role": user.role,
                    "role_label": ROLE_LABELS.get(user.role, user.role),
                    "permission_names": permission_names,
                    "employee_id": user.employee_id,
                    "name": emp.name if emp else "",
                    "title": (
                        emp.job_title_rel.name
                        if emp and emp.job_title_rel
                        else (emp.title if emp else "")
                    ),
                },
            }
        )
        set_access_token_cookie(response, new_token)
        return response
    finally:
        session.close()


# ============ Logout ============


@router.post("/logout", response_model=AuthMessageOut)
def logout(request: Request):
    """登出：清除 access_token 和 admin_token Cookie，並廢止目前 token。"""
    # 廢止目前 token（遞增 token_version）
    token = request.cookies.get("access_token")
    if not token:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1]

    # 在 token 廢止前先抽取 audit 用的 username / user_id
    # 使用 verify_exp=False 確保即使 token 已過期也能成功抽取
    audit_username = None
    audit_user_id = None
    if token:
        from utils.auth import decode_token_for_audit

        _payload = decode_token_for_audit(token) or {}
        audit_user_id = _payload.get("user_id")
        audit_username = _payload.get("name")

    if token:
        try:
            from datetime import datetime, timezone, timedelta
            from utils.auth import (
                JWT_REFRESH_GRACE_HOURS,
                decode_token_allow_expired,
                revoke_token,
            )

            # 允許過期 token：access_token 15min 即過期，但 refresh grace 達 2h；
            # 若此處只認未過期 token，過期但在 grace 內的登出就會 silently no-op，
            # token_version 不會 bump、jti 不會入 blocklist，攻擊者拿被遺失的 cookie
            # 仍能透過 /refresh 換新。
            payload = decode_token_allow_expired(token)
            # qa-loop #14（與 #4 同根）：模擬中 access_token 的 user_id 是 target，登出應
            # 作廢真正的操作主體（admin = impersonated_by），不可 bump 無辜 target 的
            # token_version（會誤踢 target 真實使用者自己的合法 session）。模擬 token 本身
            # 仍由下方 jti 黑名單失效；非模擬請求 impersonated_by 為 None → 維持 bump 自己。
            bump_user_id = payload.get("impersonated_by") or payload.get("user_id")
            if bump_user_id:
                session = get_session()
                try:
                    user = session.query(User).filter(User.id == bump_user_id).first()
                    if user:
                        user.token_version = (user.token_version or 0) + 1
                        session.commit()
                finally:
                    session.close()
            # 除了 token_version 整批廢止外，把當前 jti 寫入黑名單；防護精細到
            # 單一 token，且涵蓋無 user_id 的 guest token 場景。
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
                # 寬限期內仍可換發，所以 expires_at 設為 exp + grace
                revoke_token(
                    jti,
                    exp_dt + timedelta(hours=JWT_REFRESH_GRACE_HOURS),
                    reason="logout",
                )
        except HTTPException:
            # token 超出 grace 或已被廢止：登出本身仍要成功（清 cookie），
            # 但因 token 早已失效，無需再次廢止。
            pass
        except Exception as e:
            logger.warning("logout 廢止 token 失敗（將仍清 cookie）：%s", e)

    # 只有 token 存在時才寫 LOGOUT audit
    # Why: 無 token 的 /logout 請求（爬蟲/curl 探測）會產生 username=None 的雜訊
    # audit 行；登入過的使用者主動登出才是有意義的事件。
    if token:
        write_login_audit(
            request,
            action="LOGOUT",
            username=audit_username,
            user_id=audit_user_id,
        )

    # Spec F: revoke current staff refresh family on logout
    _staff_refresh_raw = request.cookies.get("staff_refresh_token")
    if _staff_refresh_raw:
        try:
            from hashlib import sha256 as _sha256

            from models.staff_refresh_token import StaffRefreshToken as _SRT

            _h = _sha256(_staff_refresh_raw.encode()).hexdigest()
            _srt_session = get_session()
            try:
                _rt = _srt_session.query(_SRT).filter(_SRT.token_hash == _h).first()
                if _rt:
                    from services.staff_refresh import revoke_family as _revoke_family

                    _revoke_family(_rt.user_id, _rt.family_id)
            finally:
                _srt_session.close()
        except Exception as _e:
            logger.warning(
                "logout 撤銷 staff refresh family 失敗（仍清 cookie）：%s", _e
            )

    response = JSONResponse(content={"message": "已登出"})
    clear_access_token_cookie(response)
    clear_admin_token_cookie(response)
    clear_staff_refresh_cookie(response)
    return response


# ============ Sessions Management (Spec F) ============


def _reject_if_impersonating(current_user: dict) -> None:
    """模擬（impersonation）期間禁止操作「自己的」session 管理端點。

    qa-loop round2（2026-06-29）：模擬中 access_token 的 user_id = 被模擬 target，
    current_user['user_id'] 解析為 target → list/revoke/logout-all 會誤作用到無辜 target
    （洩漏其裝置 IP/UA，或強制登出其所有裝置 + bump 其 token_version）。唯讀 PORTAL_PREVIEW
    預覽更不應觸發任何寫入。操作者要管理自己的 session 應先結束模擬。
    """
    if current_user.get("impersonated_by") is not None:
        raise HTTPException(
            status_code=403,
            detail="模擬（impersonation）期間不可管理 session，請先結束模擬",
        )


@router.get("/sessions", response_model=list[SessionItemOut])
def list_my_sessions(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """列出目前使用者的所有 active StaffRefreshToken family，含 is_current 標記當前裝置。

    Spec F §3.4 (audit P1 #11)
    """
    _reject_if_impersonating(current_user)
    raw = request.cookies.get("staff_refresh_token")
    current_family = None
    if raw:
        h = sha256(raw.encode()).hexdigest()
        _session = get_session()
        try:
            rt = _session.query(StaffRefreshToken).filter_by(token_hash=h).first()
            if rt:
                current_family = rt.family_id
        finally:
            _session.close()

    session = get_session()
    try:
        sql = text("""
            SELECT family_id, MAX(created_at) AS last_active,
                   MAX(user_agent) AS user_agent, MAX(ip) AS ip,
                   COUNT(*) AS token_count
            FROM staff_refresh_tokens
            WHERE user_id = :uid AND revoked_at IS NULL AND expires_at > :now
            GROUP BY family_id
            ORDER BY last_active DESC
            """)
        rows = session.execute(
            sql,
            {"uid": current_user["user_id"], "now": now_taipei_naive()},
        ).all()
        return [
            SessionItemOut(
                family_id=r.family_id,
                last_active=r.last_active,
                user_agent=r.user_agent,
                ip=r.ip,
                token_count=r.token_count,
                is_current=(r.family_id == current_family),
            )
            for r in rows
        ]
    finally:
        session.close()


@router.delete("/sessions/{family_id}", response_model=RevokeSessionOut)
def revoke_session(
    family_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Per-session revoke：撤銷指定 family 的所有 token（其他 family 不影響）。

    Spec F §3.4 (audit P1 #11)
    """
    _reject_if_impersonating(current_user)
    from services.staff_refresh import revoke_family

    n = revoke_family(current_user["user_id"], family_id)
    if n == 0:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"revoked": n}


@router.post("/sessions/logout-all", response_model=LogoutAllSessionsOut)
def logout_all_sessions(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Logout-all：revoke 所有 family + bump token_version + clear self cookies。

    Spec F §3.4 (audit P1 #11)
    """
    _reject_if_impersonating(current_user)
    from services.staff_refresh import revoke_all_for_user

    revoke_all_for_user(current_user["user_id"])
    response = JSONResponse(content={"logout_all": True})
    clear_staff_refresh_cookie(response)
    clear_access_token_cookie(response)
    return response


# ============ End Impersonate ============


@router.post("/end-impersonate", response_model=AuthUserResultOut)
def end_impersonate(request: Request):
    """結束冒充：將 admin_token Cookie 還原為 access_token，清除 admin_token。

    回傳管理員的 user 資訊供前端更新 UI。
    """
    admin_token = request.cookies.get("admin_token")
    if not admin_token:
        raise HTTPException(status_code=400, detail="目前不在冒充狀態")

    # 驗證 admin_token 仍然有效
    from utils.auth import decode_token_allow_expired

    payload = decode_token_allow_expired(admin_token)

    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token 資料不完整")

    session = get_session()
    try:
        user = (
            session.query(User)
            .filter(User.id == user_id, User.is_active == True)
            .first()
        )
        if not user:
            raise HTTPException(status_code=401, detail="管理員帳號已停用或不存在")

        # #8：admin_token cookie 由 impersonate 端點以「操作者自己的 token」簽發備份，
        # 經 JWT 簽章無法偽造。發起模擬需 PORTAL_PREVIEW（readonly）或 PORTAL_IMPERSONATE
        # （write），故合法發起者（admin / principal 等）皆持其一。原本硬要求
        # role=='admin' 會把園長(principal) readonly 模擬鎖死在模擬狀態（按「結束預覽」
        # 403、唯一逃生口是整個登出）。改以模擬權限判定，admin（wildcard）仍涵蓋。
        operator_perms = resolve_user_permissions(user, session)
        if not (
            has_permission(operator_perms, Permission.PORTAL_PREVIEW)
            or has_permission(operator_perms, Permission.PORTAL_IMPERSONATE)
        ):
            raise HTTPException(
                status_code=403, detail="無效的管理員 Token（無模擬權限）"
            )

        # C14：校驗 admin_token 的 token_version 與 DB 一致，否則密碼變更/重設/撤帳
        # bump token_version（全域作廢）後，舊 admin_token cookie 仍可換發新 admin token
        # 繞過作廢。與 refresh 路徑（驗 token_version）對齊；payload 缺欄位視為 0。
        if payload.get("token_version", 0) != (user.token_version or 0):
            raise HTTPException(
                status_code=401, detail="管理員 Token 已失效，請重新登入"
            )

        # #9：撤銷「當前模擬 access_token」的 jti，避免殘留 cookie 在 ~15min 自然
        # 過期前仍具被模擬者權限（對齊 logout 的 jti 黑名單）。不 bump 被模擬者
        # token_version——那會誤踢 target 真實使用者自己的合法 session（同 logout #14）。
        imp_token = request.cookies.get("access_token")
        if imp_token:
            try:
                from datetime import datetime, timedelta, timezone

                from utils.auth import (
                    JWT_REFRESH_GRACE_HOURS,
                    decode_token_allow_expired,
                    revoke_token,
                )

                imp_payload = decode_token_allow_expired(imp_token)
                imp_jti = imp_payload.get("jti")
                imp_exp = imp_payload.get("exp")
                if imp_jti and imp_exp:
                    revoke_token(
                        imp_jti,
                        datetime.fromtimestamp(imp_exp, tz=timezone.utc)
                        + timedelta(hours=JWT_REFRESH_GRACE_HOURS),
                        reason="end_impersonate",
                    )
            except HTTPException:
                # 模擬 token 已過期超出寬限或已撤銷：結束模擬仍要成功
                pass
            except Exception as e:  # noqa: BLE001 — 撤銷失敗不能阻擋退出模擬
                logger.warning("end_impersonate 撤銷模擬 token 失敗（仍繼續）：%s", e)

        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()
        permission_names = operator_perms

        # 為管理員簽發新的 access token（避免使用可能已接近過期的舊 token）
        new_token = create_access_token(
            {
                "user_id": user.id,
                "employee_id": user.employee_id,
                "role": user.role,
                "name": emp.name if emp else "",
                "permission_names": permission_names,
                "token_version": user.token_version,
            }
        )

        request.state.audit_summary = f"[結束冒充] 恢復為管理員 {emp.name if emp else user.username}（user_id={user.id}）"

        response = JSONResponse(
            content={
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "role": user.role,
                    "role_label": ROLE_LABELS.get(user.role, user.role),
                    "permission_names": permission_names,
                    "employee_id": user.employee_id,
                    "name": emp.name if emp else "",
                    "title": (
                        emp.job_title_rel.name
                        if emp and emp.job_title_rel
                        else (emp.title if emp else "")
                    ),
                },
            }
        )
        set_access_token_cookie(response, new_token)
        clear_admin_token_cookie(response)
        return response
    finally:
        session.close()


# ============ Protected Routes ============


@router.get("/me", response_model=AuthUserOut)
def get_me(current_user: dict = Depends(get_current_user)):
    """取得目前登入者資訊"""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(status_code=404, detail=USER_NOT_FOUND)
        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()
        permission_names = resolve_user_permissions(user, session)
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "role_label": ROLE_LABELS.get(user.role, user.role),
            "permission_names": permission_names,
            "employee_id": user.employee_id,
            "name": emp.name if emp else "",
            "title": (
                emp.job_title_rel.name
                if emp and emp.job_title_rel
                else (emp.title if emp else "")
            ),
            "impersonation_mode": current_user.get("impersonation_mode"),
        }
    finally:
        session.close()


@router.post("/change-password", response_model=AuthMessageOut)
def change_password(
    data: ChangePasswordRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """修改密碼

    Why issue new token on success：使用者自己改密碼後 token_version 遞增，
    若 response 不發新 token 則 client 帶舊 token 立刻 401，被迫 re-login
    （bug sweep round 4 F-PRE-1，pre-existing 自 2026-05-13 commit 3e728d2b）。
    管理員代為重設（reset_password）則維持「強制當事人下次登入」語意，不發
    新 token。

    qa-loop 廣掃（2026-07-01 P1）：全程用 current_user['user_id']（模擬期間為被
    冒充的 target），比照 /sessions 三端點加上模擬守衛，禁止在模擬期間真的改掉
    無辜 target 的密碼/token_version/refresh family。
    """
    _reject_if_impersonating(current_user)

    client_ip = get_client_ip(request) or "unknown"
    user_id = current_user["user_id"]
    username_for_audit = current_user.get("username", "")

    # 雙層限流：IP 滑動視窗 + per-user 失敗鎖定（DB-backed，與 login scope 隔離）
    try:
        _check_pwd_change_ip(client_ip)
    except HTTPException:
        write_login_audit(
            request,
            action="PASSWORD_CHANGE_RATE_LIMITED",
            username=username_for_audit,
            extras={"ip": client_ip, "scope": "pwd_change_ip"},
        )
        raise
    try:
        _check_pwd_change_user_lockout(user_id)
    except HTTPException:
        write_login_audit(
            request,
            action="PASSWORD_CHANGE_LOCKED",
            username=username_for_audit,
            extras={"user_id": user_id, "scope": "pwd_change_user"},
        )
        raise

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail=USER_NOT_FOUND)
        if not verify_password(data.old_password, user.password_hash):
            _record_pwd_change_failure(user_id)  # 記失敗 → 累積觸發 lockout
            raise HTTPException(status_code=400, detail="舊密碼錯誤")
        validate_password_strength(data.new_password)
        assert_not_recently_used(session, user.id, data.new_password)
        new_hash = hash_password(data.new_password)
        user.password_hash = new_hash
        record_password_history(session, user.id, new_hash)
        user.must_change_password = False  # 使用者主動修改後清除強制旗標
        # 與 reset_password 對齊：密碼變更後遞增 token_version，使所有現有 session
        # 在下次 refresh 時即被拒絕；防止帳號疑似外洩後舊 token 在 grace 期內仍可用。
        user.token_version = (user.token_version or 0) + 1
        # 同 session（與 token_version bump 同 transaction）撤銷該使用者所有 staff_refresh
        # family，使其他裝置/session 的舊 refresh token 立即失效——補上 /auth/refresh 的
        # staff rotation 路徑不檢查 token_version（family 僅靠 revoke 失效）的缺口。
        # 不用 revoke_all_for_user（它另開 session 且會重複 bump token_version，使本次
        # 重發的新 access token 反而 stale）。
        session.query(StaffRefreshToken).filter(
            StaffRefreshToken.user_id == user.id,
            StaffRefreshToken.revoked_at.is_(None),
        ).update(
            {"revoked_at": now_taipei_naive()},
            synchronize_session=False,
        )

        # 為當事人發新 token（同步新 token_version + must_change_password=False），
        # 避免「改完密碼立刻被踢」。其他 session 的舊 token 仍會在下次 refresh 被拒。
        permission_names = resolve_user_permissions(user, session)
        emp = (
            session.query(Employee).filter(Employee.id == user.employee_id).first()
            if user.employee_id
            else None
        )
        new_token = create_access_token(
            {
                "user_id": user.id,
                "employee_id": user.employee_id,
                "role": user.role,
                "name": emp.name if emp else "",
                "permission_names": permission_names,
                "token_version": user.token_version,
            }
        )
        session.commit()
        _clear_pwd_change_failures(user_id)  # 成功後清失敗計數（commit 後才 clear）

        response = JSONResponse(content={"message": "密碼修改成功"})
        set_access_token_cookie(response, new_token)
        # P3（qa-loop round2 2026-06-29）：上面已撤「當前裝置」的 staff_refresh family 卻只重發
        # access token，未重發 refresh → 當前裝置在 access token（15min）到期後 /refresh 撞已撤
        # family 被踢，與「避免改完密碼立刻被踢」的意圖矛盾。比照 login 為當前裝置重新簽發 refresh
        # family（新 family 於撤銷後建立、未被 revoke，故有效）。
        from services.staff_refresh import issue_refresh_token as _issue_staff_refresh

        _refresh_raw, _ = _issue_staff_refresh(
            user_id=user.id,
            user_agent=request.headers.get("user-agent") or "",
            ip=client_ip,
        )
        set_staff_refresh_cookie(response, _refresh_raw)
        return response
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


# ============ Admin Routes ============


@router.get("/users", response_model=list[AuthAdminUserItemOut])
def list_users(
    current_user: dict = Depends(
        require_staff_permission(Permission.USER_MANAGEMENT_READ)
    ),
):
    """列出所有使用者"""
    session = get_session()
    try:
        users = (
            session.query(User, Employee)
            .outerjoin(Employee, User.employee_id == Employee.id)
            .all()
        )
        return [
            {
                "id": u.id,
                "username": u.username,
                "role": u.role,
                "role_label": ROLE_LABELS.get(u.role, u.role),
                "permission_names": resolve_user_permissions(u, session),
                "is_active": u.is_active,
                "employee_id": u.employee_id,
                "employee_name": emp.name if emp else "",
                "last_login": u.last_login.isoformat() if u.last_login else None,
            }
            for u, emp in users
        ]
    finally:
        session.close()


@router.post("/users", status_code=201, response_model=MutationResultOut)
def create_user(
    data: CreateUserRequest,
    current_user: dict = Depends(
        require_staff_permission(Permission.USER_MANAGEMENT_WRITE)
    ),
):
    """建立使用者帳號"""
    session = get_session()
    try:
        _assert_can_manage_user(
            current_user,
            session=session,
            payload_role=data.role,
            payload_permission_names=data.permission_names,
        )
        # RA-HIGH-1b：驗證 permission_names code/scope 格式（非 scope-aware code 不可
        # 帶 scope 後綴、scope 值須合法、code 須存在）。早於密碼強度檢查確保回 422。
        if data.permission_names is not None:
            bad = validate_permission_names(data.permission_names)
            if bad:
                raise HTTPException(status_code=422, detail=f"非法權限項：{bad}")
        if session.query(User).filter(User.username == data.username).first():
            raise HTTPException(status_code=400, detail="帳號已存在")

        if data.employee_id is not None:
            if session.query(User).filter(User.employee_id == data.employee_id).first():
                raise HTTPException(status_code=400, detail="該員工已有帳號")
            emp = (
                session.query(Employee).filter(Employee.id == data.employee_id).first()
            )
            if not emp:
                raise HTTPException(status_code=404, detail=EMPLOYEE_DOES_NOT_EXIST)
        else:
            emp = None

        # 計算權限：若有指定則使用，否則套用角色預設
        if data.permission_names is not None:
            final_permission_names = data.permission_names
        else:
            final_permission_names = get_role_default_permissions(session, data.role)

        # 驗證密碼強度
        validate_password_strength(data.password)

        new_hash = hash_password(data.password)
        user = User(
            employee_id=data.employee_id,
            username=data.username,
            password_hash=new_hash,
            role=data.role,
            permission_names=final_permission_names,
            must_change_password=True,  # 新帳號強制首次登入修改密碼
        )
        session.add(user)
        session.flush()  # 取 user.id 給 password_history FK
        record_password_history(session, user.id, new_hash)
        session.commit()
        return {"message": "帳號建立成功", "id": user.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.put("/users/{user_id}/reset-password", response_model=DeleteResultOut)
def reset_password(
    user_id: int,
    data: ResetPasswordRequest,
    request: Request,
    current_user: dict = Depends(
        require_staff_permission(Permission.USER_MANAGEMENT_WRITE)
    ),
):
    """重設密碼（admin 代為操作）"""
    client_ip = get_client_ip(request) or "unknown"
    try:
        _check_pwd_reset_ip(client_ip)  # 防 admin cookie 被竊狂刷別人
    except HTTPException:
        write_login_audit(
            request,
            action="PASSWORD_RESET_RATE_LIMITED",
            username=current_user.get("username", ""),
            extras={
                "ip": client_ip,
                "scope": "pwd_reset_ip",
                "target_user_id": user_id,
            },
        )
        raise

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail=USER_NOT_FOUND)

        _assert_can_manage_user(current_user, session=session, target_user=user)

        validate_password_strength(data.new_password)
        assert_not_recently_used(session, user.id, data.new_password)
        new_hash = hash_password(data.new_password)
        user.password_hash = new_hash
        record_password_history(session, user.id, new_hash)
        user.must_change_password = True  # 管理員代為重設密碼，強制當事人下次登入修改
        user.token_version = (
            user.token_version or 0
        ) + 1  # 使所有現有 session 的 token 立即無法刷新
        session.commit()
        return {"message": "密碼重設成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/permissions", response_model=AuthPermissionsDefinitionOut)
def get_permissions(current_user: dict = Depends(get_current_user)):
    """取得權限定義（供前端渲染 UI）— 從 DB 拉，admin runtime 改動立即生效。

    R6-1：須登入才可讀（原本無任何 Depends → 匿名訪客可拉整份 RBAC 模型，含
    自訂角色 code/label/權限陣列）。要求 get_current_user 即封閉匿名洩漏。
    """
    session = get_session()
    try:
        return get_permissions_definition(session)
    finally:
        session.close()


@router.put("/users/{user_id}", response_model=DeleteResultOut)
def update_user(
    user_id: int,
    data: UpdateUserRequest,
    request: Request,
    current_user: dict = Depends(
        require_staff_permission(Permission.USER_MANAGEMENT_WRITE)
    ),
):
    """更新使用者角色與權限"""
    # 禁止管理員停用自己的帳號（防止系統鎖死）
    if user_id == current_user.get("user_id") and data.is_active is False:
        raise HTTPException(status_code=400, detail="不可停用自己的帳號")

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail=USER_NOT_FOUND)

        _assert_can_manage_user(
            current_user,
            session=session,
            target_user=user,
            payload_role=data.role,
            payload_permission_names=data.permission_names,
        )
        # RA-HIGH-1b：驗證 permission_names code/scope 格式（同 create_user）。
        if data.permission_names is not None:
            bad = validate_permission_names(data.permission_names)
            if bad:
                raise HTTPException(status_code=422, detail=f"非法權限項：{bad}")

        # 記錄舊值，用於審計摘要
        old_role = user.role
        old_permission_names = user.permission_names
        old_is_active = user.is_active

        if data.role is not None:
            user.role = data.role
            # 角色變更時，若未指定權限則套用新角色的預設權限
            if data.permission_names is None:
                user.permission_names = get_role_default_permissions(session, data.role)

        if data.permission_names is not None:
            user.permission_names = data.permission_names

        if data.is_active is not None:
            user.is_active = data.is_active
            # 帳號停用（True → False）為軟刪語意，顯式標記
            if not user.is_active and old_is_active:
                mark_soft_delete(request, "user", user.username or f"#{user.id}")

        # 帳號停用、角色或權限實際變更時，遞增 token_version
        # 使所有已持有的 token 在下次嘗試 refresh 時立即被拒絕（最長 15 分鐘後完全失效）
        should_revoke = (
            (not user.is_active and old_is_active)
            or (user.role != old_role)
            or (user.permission_names != old_permission_names)
        )
        if should_revoke:
            user.token_version = (user.token_version or 0) + 1
            # F2/F3：同步撤銷該 user 所有 staff_refresh family（比照 change_password）。
            # rotation 路徑不檢 token_version，僅 family revoke 能讓停用/改權後的既有
            # refresh token 立即失效——否則停用→re-enable 後失竊 cookie 復活（F2），
            # 改角色/權限後既有 session 不終止（F3）。
            session.query(StaffRefreshToken).filter(
                StaffRefreshToken.user_id == user.id,
                StaffRefreshToken.revoked_at.is_(None),
            ).update(
                {"revoked_at": now_taipei_naive()},
                synchronize_session=False,
            )

        # 建立變更摘要。
        # 注意：純停用（軟刪）已由 mark_soft_delete 設定 audit_summary，
        # 維持既有 middleware 稽核路徑；role/permission 變更（提權敏感）則改為
        # in-session 寫入，與主交易共生死（對齊金流 write_audit_in_session pattern）。
        changes = []
        changes_payload: dict = {}
        if data.role is not None and user.role != old_role:
            changes.append(f"角色 {old_role} → {user.role}")
            changes_payload["old_role"] = old_role
            changes_payload["new_role"] = user.role
        if user.permission_names != old_permission_names:
            old_set = set(old_permission_names or [])
            new_set = set(user.permission_names or [])
            added = sorted(new_set - old_set)
            removed = sorted(old_set - new_set)
            if added or removed:
                changes.append(f"權限變更: +{added} / -{removed}")
                changes_payload["permissions_added"] = added
                changes_payload["permissions_removed"] = removed
            else:
                changes.append("權限未變更")
        if data.is_active is not None and user.is_active != old_is_active:
            if user.is_active:
                # 啟用帳號：一般變更摘要
                changes.append("帳號啟用")
                changes_payload["is_active"] = True
            else:
                # 純停用走 mark_soft_delete → middleware 路徑（changes 維持空）；
                # 與 role/permission 變更同時發生時，併入 in-session 摘要避免遺漏
                changes_payload["is_active"] = False
        if changes:
            if not hasattr(request.state, "audit_summary"):
                request.state.audit_summary = "修改使用者帳號：" + "、".join(changes)
            request.state.audit_entity_id = str(user.id)
            summary_parts = list(changes)
            if not user.is_active and old_is_active:
                summary_parts.append("帳號停用（軟刪）")
            changes_payload["token_revoked"] = bool(should_revoke)
            # 同交易內寫 AuditLog（提權路徑：主資料 + 稽核共生死，避免 middleware
            # fire-and-forget 在 threadpool 故障時丟稽核）。write_audit_in_session
            # 內部會設 request.state.audit_skip=True 防 middleware 二次寫入。
            write_audit_in_session(
                session,
                request,
                action="UPDATE",
                entity_type="user",
                entity_id=str(user.id),
                summary="修改使用者帳號：" + "、".join(summary_parts),
                changes=changes_payload,
            )

        session.commit()
        return {"message": "使用者已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/users/{user_id}", response_model=DeleteResultOut)
def delete_user(
    user_id: int,
    current_user: dict = Depends(
        require_staff_permission(Permission.USER_MANAGEMENT_WRITE)
    ),
):
    """刪除使用者帳號"""
    # 禁止管理員刪除自己的帳號（防止系統鎖死）
    if user_id == current_user.get("user_id"):
        raise HTTPException(status_code=400, detail="不可刪除自己的帳號")

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail=USER_NOT_FOUND)

        _assert_can_manage_user(current_user, session=session, target_user=user)

        session.delete(user)
        session.commit()
        return {"message": "帳號已刪除"}
    finally:
        session.close()
