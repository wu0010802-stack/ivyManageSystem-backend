"""
Authentication & user management router
"""

import ipaddress
import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from utils.errors import raise_safe_500
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from models.database import get_session, User, Employee
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
from utils.cookie import (
    set_access_token_cookie,
    clear_access_token_cookie,
    set_admin_token_cookie,
    clear_admin_token_cookie,
)
from utils.permissions import Permission
from utils.permissions import (
    get_permissions_definition,
    get_role_default_permissions,
    ROLE_LABELS,
)

logger = logging.getLogger(__name__)


def _get_school_wifi_networks() -> list:
    raw = os.getenv("SCHOOL_WIFI_IPS", "").strip()
    if not raw:
        return []
    result = []
    for entry in raw.split(","):
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


def _assert_can_manage_user(
    current_user: dict,
    *,
    target_user: Optional[User] = None,
    payload_role: Optional[str] = None,
    payload_permissions: Optional[int] = None,
) -> None:
    """USER_MANAGEMENT_WRITE 守衛：caller 不可超過自身權限管理他人。

    1. caller_role == 'admin' → 一律放行
    2. target_user.role == 'admin' → 拒絕（不可動 admin 帳號）
    3. payload_role == 'admin' → 拒絕（不可指定 admin 角色）
    4. 「最終權限」(payload_permissions or 角色預設) 必須 ⊆ caller_permissions
    """
    caller_role = current_user.get("role")
    if caller_role == "admin":
        return

    caller_id = current_user.get("user_id")
    caller_perms = int(current_user.get("permissions") or 0)

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

    final_perms = payload_permissions
    if final_perms is None and payload_role is not None:
        final_perms = get_role_default_permissions(payload_role)

    if final_perms is not None:
        final_perms_int = int(final_perms)
        if (final_perms_int & ~caller_perms) != 0:
            logger.warning(
                "user-management 拒絕：caller user_id=%s 權限 %s 不足以授予 %s",
                caller_id,
                caller_perms,
                final_perms_int,
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

_ip_attempts: dict[str, list[float]] = defaultdict(list)
_account_failures: dict[str, list[float]] = defaultdict(list)


def _check_ip_rate_limit(ip: str) -> None:
    """IP 層級滑動視窗限流：超出則拋 429。"""
    now = time.time()
    _ip_attempts[ip] = [t for t in _ip_attempts[ip] if now - t < _IP_WINDOW]
    if len(_ip_attempts[ip]) >= _IP_MAX_ATTEMPTS:
        logger.warning("IP 登入頻率超限: %s", ip)
        raise HTTPException(status_code=429, detail="登入嘗試次數過多，請稍後再試")
    _ip_attempts[ip].append(now)


def _check_account_lockout(username: str) -> None:
    """帳號層級失敗鎖定：連續失敗 _FAIL_THRESHOLD 次後拋 429，含剩餘解鎖時間。"""
    now = time.time()
    _account_failures[username] = [
        t for t in _account_failures[username] if now - t < _FAIL_LOCKOUT
    ]
    if len(_account_failures[username]) >= _FAIL_THRESHOLD:
        earliest = _account_failures[username][0]
        remaining_sec = int(_FAIL_LOCKOUT - (now - earliest))
        remaining_min = max(1, (remaining_sec + 59) // 60)
        logger.warning("帳號已鎖定: %s（剩餘 %d 分鐘）", username, remaining_min)
        raise HTTPException(
            status_code=429,
            detail="密碼錯誤次數過多，帳號已暫時鎖定，請稍後再試",
        )


def _record_login_failure(username: str) -> None:
    """記錄帳號登入失敗一次。"""
    _account_failures[username].append(time.time())


def _clear_login_failures(username: str) -> None:
    """登入成功後清除帳號的失敗記錄。"""
    _account_failures[username] = []


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
    permissions: Optional[int] = None  # None 表示使用角色預設權限


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    permissions: Optional[int] = None
    is_active: Optional[bool] = None


class ResetPasswordRequest(BaseModel):
    new_password: str


class ImpersonateRequest(BaseModel):
    employee_id: int


# ============ Public Routes ============


@router.post("/impersonate")
def impersonate_user(
    data: ImpersonateRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """切換使用者身份（管理員限定）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="權限不足")

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
        permissions = (
            target_user.permissions
            if target_user.permissions is not None
            else get_role_default_permissions(target_user.role)
        )
        target_token = create_access_token(
            {
                "user_id": target_user.id,
                "employee_id": target_user.employee_id,
                "role": target_user.role,
                "name": target_emp.name,
                "permissions": permissions,
                "token_version": target_user.token_version,
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
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(
            "冒充操作：操作者 user_id=%s 切換為 user_id=%s（role=%s）來源 IP=%s",
            current_user.get("user_id"),
            target_user.id,
            target_user.role,
            client_ip,
        )
        request.state.audit_summary = (
            f"[冒充] 操作者 {current_user.get('name')}（user_id={current_user.get('user_id')}）"
            f" 切換為 {target_emp.name}（user_id={target_user.id}，"
            f"{ROLE_LABELS.get(target_user.role, target_user.role)}）"
        )

        response = JSONResponse(
            content={
                "user": {
                    "id": target_user.id,
                    "username": target_user.username,
                    "role": target_user.role,
                    "role_label": ROLE_LABELS.get(target_user.role, target_user.role),
                    "permissions": permissions,
                    "employee_id": target_user.employee_id,
                    "name": target_emp.name,
                    "title": (
                        target_emp.job_title_rel.name
                        if target_emp.job_title_rel
                        else (target_emp.title or "")
                    ),
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


@router.post("/login")
def login(data: LoginRequest, request: Request):
    """教師/管理員登入"""
    client_ip = request.client.host if request.client else "unknown"
    # 層級一：IP 滑動視窗（不分成敗，防 Credential Stuffing）
    _check_ip_rate_limit(client_ip)
    # 層級二：帳號失敗鎖定（只在密碼錯誤時遞增，防定向暴力破解）
    _check_account_lockout(data.username)

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
            raise HTTPException(status_code=401, detail="帳號或密碼錯誤")

        if not verify_password(data.password, user.password_hash):
            _record_login_failure(data.username)  # 記錄失敗，累積後觸發鎖定
            raise HTTPException(status_code=401, detail="帳號或密碼錯誤")

        # 登入成功：清除帳號失敗記錄
        _clear_login_failures(data.username)

        # 教師角色須從學校 WiFi 登入
        if user.role == "teacher" and not _is_school_wifi(client_ip):
            raise HTTPException(status_code=403, detail="請連接學校 WiFi 後再登入")

        # 透明升級：若密碼是舊格式（100,000 次迭代），趁登入時無感升級至 600,000 次
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(data.password)
            logger.info("使用者 %s 密碼雜湊已自動升級至新迭代次數", user.username)

        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()

        user.last_login = datetime.now()
        session.commit()

        # permissions: -1 表示全部權限，teacher 角色不需要 permissions
        permissions = (
            user.permissions
            if user.permissions is not None
            else get_role_default_permissions(user.role)
        )

        token = create_access_token(
            {
                "user_id": user.id,
                "employee_id": user.employee_id,
                "role": user.role,
                "name": emp.name if emp else "",
                "permissions": permissions,
                "token_version": user.token_version,
            }
        )

        response = JSONResponse(
            content={
                "must_change_password": bool(user.must_change_password),
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "role": user.role,
                    "role_label": ROLE_LABELS.get(user.role, user.role),
                    "permissions": permissions,
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
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise_safe_500(e)
    finally:
        session.close()


# ============ Token Refresh ============


@router.post("/refresh")
def refresh_token(request: Request):
    """以現有 token（可為剛過期）換發新 token。
    寬限期內的過期 token 仍可刷新，超過則需重新登入。
    Token 來源：httpOnly Cookie 或 Authorization header。
    """
    # 從 Cookie 或 header 取得舊 token
    token = request.cookies.get("access_token")
    if not token:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1]
    if not token:
        raise HTTPException(status_code=401, detail="未提供認證 Token")

    # 允許過期的 token 解碼（在寬限期內）
    payload = decode_token_allow_expired(token)

    user_id = payload.get("user_id")
    if not user_id:
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
            raise HTTPException(status_code=401, detail="使用者已停用或不存在")
        if user.must_change_password:
            raise HTTPException(status_code=403, detail="需先修改密碼後才能使用系統")

        # 驗證 token_version：帳號停用或權限變更時版本遞增，使舊 token 無法換發
        # payload 缺少 token_version（舊 token 向下相容）時視為 0，與 DB 預設值相符
        if payload.get("token_version", 0) != user.token_version:
            raise HTTPException(
                status_code=401, detail="Token 已失效，請重新登入（帳號狀態已變更）"
            )

        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()
        permissions = (
            user.permissions
            if user.permissions is not None
            else get_role_default_permissions(user.role)
        )

        new_token = create_access_token(
            {
                "user_id": user.id,
                "employee_id": user.employee_id,
                "role": user.role,
                "name": emp.name if emp else "",
                "permissions": permissions,
                "token_version": user.token_version,
            }
        )

        response = JSONResponse(
            content={
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "role": user.role,
                    "role_label": ROLE_LABELS.get(user.role, user.role),
                    "permissions": permissions,
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


@router.post("/logout")
def logout(request: Request):
    """登出：清除 access_token 和 admin_token Cookie，並廢止目前 token。"""
    # 廢止目前 token（遞增 token_version）
    token = request.cookies.get("access_token")
    if not token:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1]

    if token:
        try:
            from datetime import datetime, timezone
            from utils.auth import (
                JWT_REFRESH_GRACE_HOURS,
                decode_token,
                revoke_token,
            )

            payload = decode_token(token)
            user_id = payload.get("user_id")
            if user_id:
                session = get_session()
                try:
                    user = session.query(User).filter(User.id == user_id).first()
                    if user:
                        user.token_version = (user.token_version or 0) + 1
                        session.commit()
                finally:
                    session.close()
            # LOW-2：除了 token_version 整批廢止外，把當前 jti 寫入黑名單
            #   防護精細到單一 token，且涵蓋無 user_id 的 guest token 場景
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
                # 寬限期內仍可換發，所以 expires_at 設為 exp + grace
                from datetime import timedelta

                revoke_token(
                    jti,
                    exp_dt + timedelta(hours=JWT_REFRESH_GRACE_HOURS),
                    reason="logout",
                )
        except Exception:
            pass  # token 已過期或無效，無需廢止

    response = JSONResponse(content={"message": "已登出"})
    clear_access_token_cookie(response)
    clear_admin_token_cookie(response)
    return response


# ============ End Impersonate ============


@router.post("/end-impersonate")
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

        if user.role != "admin":
            raise HTTPException(
                status_code=403, detail="無效的管理員 Token（角色非 admin）"
            )

        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()
        permissions = (
            user.permissions
            if user.permissions is not None
            else get_role_default_permissions(user.role)
        )

        # 為管理員簽發新的 access token（避免使用可能已接近過期的舊 token）
        new_token = create_access_token(
            {
                "user_id": user.id,
                "employee_id": user.employee_id,
                "role": user.role,
                "name": emp.name if emp else "",
                "permissions": permissions,
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
                    "permissions": permissions,
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


@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    """取得目前登入者資訊"""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(status_code=404, detail=USER_NOT_FOUND)
        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()
        permissions = (
            user.permissions
            if user.permissions is not None
            else get_role_default_permissions(user.role)
        )
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "role_label": ROLE_LABELS.get(user.role, user.role),
            "permissions": permissions,
            "employee_id": user.employee_id,
            "name": emp.name if emp else "",
            "title": (
                emp.job_title_rel.name
                if emp and emp.job_title_rel
                else (emp.title if emp else "")
            ),
        }
    finally:
        session.close()


@router.post("/change-password")
def change_password(
    data: ChangePasswordRequest, current_user: dict = Depends(get_current_user)
):
    """修改密碼"""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(status_code=404, detail=USER_NOT_FOUND)
        if not verify_password(data.old_password, user.password_hash):
            raise HTTPException(status_code=400, detail="舊密碼錯誤")
        validate_password_strength(data.new_password)
        user.password_hash = hash_password(data.new_password)
        user.must_change_password = False  # 使用者主動修改後清除強制旗標
        session.commit()
        return {"message": "密碼修改成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


# ============ Admin Routes ============


@router.get("/users")
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
                "permissions": (
                    u.permissions
                    if u.permissions is not None
                    else get_role_default_permissions(u.role)
                ),
                "is_active": u.is_active,
                "employee_id": u.employee_id,
                "employee_name": emp.name if emp else "",
                "last_login": u.last_login.isoformat() if u.last_login else None,
            }
            for u, emp in users
        ]
    finally:
        session.close()


@router.post("/users", status_code=201)
def create_user(
    data: CreateUserRequest,
    current_user: dict = Depends(
        require_staff_permission(Permission.USER_MANAGEMENT_WRITE)
    ),
):
    """建立使用者帳號"""
    _assert_can_manage_user(
        current_user,
        payload_role=data.role,
        payload_permissions=data.permissions,
    )

    session = get_session()
    try:
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
        if data.permissions is not None:
            final_permissions = data.permissions
        else:
            final_permissions = get_role_default_permissions(data.role)

        # 驗證密碼強度
        validate_password_strength(data.password)

        user = User(
            employee_id=data.employee_id,
            username=data.username,
            password_hash=hash_password(data.password),
            role=data.role,
            permissions=final_permissions,
            must_change_password=True,  # 新帳號強制首次登入修改密碼
        )
        session.add(user)
        session.commit()
        return {"message": "帳號建立成功", "id": user.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.put("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    data: ResetPasswordRequest,
    current_user: dict = Depends(
        require_staff_permission(Permission.USER_MANAGEMENT_WRITE)
    ),
):
    """重設密碼"""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail=USER_NOT_FOUND)

        _assert_can_manage_user(current_user, target_user=user)

        validate_password_strength(data.new_password)
        user.password_hash = hash_password(data.new_password)
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


@router.get("/permissions")
def get_permissions():
    """取得權限定義（供前端渲染 UI）"""
    return get_permissions_definition()


@router.put("/users/{user_id}")
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
            target_user=user,
            payload_role=data.role,
            payload_permissions=data.permissions,
        )

        # 記錄舊值，用於審計摘要
        old_role = user.role
        old_permissions = user.permissions
        old_is_active = user.is_active

        if data.role is not None:
            user.role = data.role
            # 角色變更時，若未指定權限則套用新角色的預設權限
            if data.permissions is None:
                user.permissions = get_role_default_permissions(data.role)

        if data.permissions is not None:
            user.permissions = data.permissions

        if data.is_active is not None:
            user.is_active = data.is_active

        # 帳號停用、角色或權限實際變更時，遞增 token_version
        # 使所有已持有的 token 在下次嘗試 refresh 時立即被拒絕（最長 15 分鐘後完全失效）
        should_revoke = (
            (not user.is_active and old_is_active)
            or (user.role != old_role)
            or (user.permissions != old_permissions)
        )
        if should_revoke:
            user.token_version = (user.token_version or 0) + 1

        # 建立變更摘要並傳給 AuditMiddleware
        changes = []
        if data.role is not None and user.role != old_role:
            changes.append(f"角色 {old_role} → {user.role}")
        if user.permissions != old_permissions:
            changes.append(f"權限遮罩 {old_permissions} → {user.permissions}")
        if data.is_active is not None and user.is_active != old_is_active:
            changes.append("帳號" + ("啟用" if user.is_active else "停用"))
        if changes:
            request.state.audit_summary = "修改使用者帳號：" + "、".join(changes)

        session.commit()
        return {"message": "使用者已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/users/{user_id}")
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

        _assert_can_manage_user(current_user, target_user=user)

        session.delete(user)
        session.commit()
        return {"message": "帳號已刪除"}
    finally:
        session.close()
