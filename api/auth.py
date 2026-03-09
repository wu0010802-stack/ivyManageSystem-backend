"""
Authentication & user management router
"""

import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from models.database import get_session, User, Employee
from utils.auth import (
    hash_password, verify_password, needs_rehash, create_access_token,
    get_current_user, decode_token_allow_expired, require_permission,
)
from utils.permissions import Permission
from utils.permissions import get_permissions_definition, get_role_default_permissions, ROLE_LABELS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

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

_IP_WINDOW = 300        # IP 滑動視窗長度：5 分鐘
_IP_MAX_ATTEMPTS = 20   # 同 IP 視窗內最多嘗試次數
_FAIL_THRESHOLD = 5     # 帳號連續失敗次數上限
_FAIL_LOCKOUT = 900     # 帳號鎖定時間：15 分鐘

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
            detail=f"密碼錯誤次數過多，帳號已暫時鎖定，請 {remaining_min} 分鐘後再試",
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
def impersonate_user(data: ImpersonateRequest, request: Request, current_user: dict = Depends(get_current_user)):
    """切換使用者身份（管理員限定）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="權限不足")

    session = get_session()
    try:
        # 1. 檢查目標員工是否存在
        target_emp = session.query(Employee).filter(Employee.id == data.employee_id).first()
        if not target_emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        # 2. 尋找該員工的使用者帳號
        target_user = session.query(User).filter(User.employee_id == data.employee_id).first()

        # 3. 如果沒有使用者帳號，拒絕切換
        if not target_user:
            raise HTTPException(status_code=400, detail="該員工沒有使用者帳號，無法切換")

        # 4. 禁止冒充 admin（防止平級或提權冒充）
        if target_user.role == "admin":
            logger.warning(
                "冒充被拒（目標為 admin）：操作者 user_id=%s 嘗試冒充 user_id=%s",
                current_user.get("user_id"), target_user.id,
            )
            raise HTTPException(status_code=403, detail="不可冒充管理員帳號")

        # 4.5 禁止冒充已停用帳號（停用後應立即失效，不因冒充繞過）
        if not target_user.is_active:
            logger.warning(
                "冒充被拒（帳號已停用）：操作者 user_id=%s 嘗試冒充已停用 user_id=%s",
                current_user.get("user_id"), target_user.id,
            )
            raise HTTPException(status_code=403, detail="無法冒充已停用的帳號")

        # 5. 產生該使用者的 token
        permissions = target_user.permissions if target_user.permissions is not None else get_role_default_permissions(target_user.role)
        token = create_access_token({
            "user_id": target_user.id,
            "employee_id": target_user.employee_id,
            "role": target_user.role,
            "name": target_emp.name,
            "permissions": permissions,
            "token_version": target_user.token_version,
        })

        # 6. 寫入審計日誌（明確標記操作者與被冒充對象，供事後追查）
        logger.info(
            "冒充操作：操作者 user_id=%s 切換為 user_id=%s（role=%s）",
            current_user.get("user_id"), target_user.id, target_user.role,
        )
        request.state.audit_summary = (
            f"[冒充] 操作者 {current_user.get('name')}（user_id={current_user.get('user_id')}）"
            f" 切換為 {target_emp.name}（user_id={target_user.id}，"
            f"{ROLE_LABELS.get(target_user.role, target_user.role)}）"
        )

        return {
            "token": token,
            "user": {
                "id": target_user.id,
                "username": target_user.username,
                "role": target_user.role,
                "role_label": ROLE_LABELS.get(target_user.role, target_user.role),
                "permissions": permissions,
                "employee_id": target_user.employee_id,
                "name": target_emp.name,
                "title": (target_emp.job_title_rel.name if target_emp.job_title_rel else (target_emp.title or "")),
            },
        }
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
        user = session.query(User).filter(
            User.username == data.username,
            User.is_active == True,
        ).first()

        if not user or not verify_password(data.password, user.password_hash):
            _record_login_failure(data.username)  # 記錄失敗，累積後觸發鎖定
            raise HTTPException(status_code=401, detail="帳號或密碼錯誤")

        # 登入成功：清除帳號失敗記錄
        _clear_login_failures(data.username)

        # 透明升級：若密碼是舊格式（100,000 次迭代），趁登入時無感升級至 600,000 次
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(data.password)
            logger.info("使用者 %s 密碼雜湊已自動升級至新迭代次數", user.username)

        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()

        user.last_login = datetime.now()
        session.commit()

        # permissions: -1 表示全部權限，teacher 角色不需要 permissions
        permissions = user.permissions if user.permissions is not None else get_role_default_permissions(user.role)

        token = create_access_token({
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": emp.name if emp else "",
            "permissions": permissions,
            "token_version": user.token_version,
        })

        return {
            "token": token,
            "must_change_password": bool(user.must_change_password),
            "user": {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "role_label": ROLE_LABELS.get(user.role, user.role),
                "permissions": permissions,
                "employee_id": user.employee_id,
                "name": emp.name if emp else "",
                "title": (emp.job_title_rel.name if emp and emp.job_title_rel else (emp.title if emp else "")),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ============ Token Refresh ============

@router.post("/refresh")
def refresh_token(authorization: str = Header(None)):
    """以現有 token（可為剛過期）換發新 token。
    寬限期內的過期 token 仍可刷新，超過則需重新登入。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供認證 Token")
    token = authorization.split(" ", 1)[1]

    # 允許過期的 token 解碼（在寬限期內）
    payload = decode_token_allow_expired(token)

    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token 資料不完整")

    # 驗證使用者仍然有效
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id, User.is_active == True).first()
        if not user:
            raise HTTPException(status_code=401, detail="使用者已停用或不存在")

        # 驗證 token_version：帳號停用或權限變更時版本遞增，使舊 token 無法換發
        # payload 缺少 token_version（舊 token 向下相容）時視為 0，與 DB 預設值相符
        if payload.get("token_version", 0) != user.token_version:
            raise HTTPException(status_code=401, detail="Token 已失效，請重新登入（帳號狀態已變更）")

        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()
        permissions = user.permissions if user.permissions is not None else get_role_default_permissions(user.role)

        new_token = create_access_token({
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": emp.name if emp else "",
            "permissions": permissions,
            "token_version": user.token_version,
        })

        return {
            "token": new_token,
            "user": {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "role_label": ROLE_LABELS.get(user.role, user.role),
                "permissions": permissions,
                "employee_id": user.employee_id,
                "name": emp.name if emp else "",
                "title": (emp.job_title_rel.name if emp and emp.job_title_rel else (emp.title if emp else "")),
            },
        }
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
            raise HTTPException(status_code=404, detail="使用者不存在")
        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()
        permissions = user.permissions if user.permissions is not None else get_role_default_permissions(user.role)
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "role_label": ROLE_LABELS.get(user.role, user.role),
            "permissions": permissions,
            "employee_id": user.employee_id,
            "name": emp.name if emp else "",
            "title": (emp.job_title_rel.name if emp and emp.job_title_rel else (emp.title if emp else "")),
        }
    finally:
        session.close()


@router.post("/change-password")
def change_password(data: ChangePasswordRequest, current_user: dict = Depends(get_current_user)):
    """修改密碼"""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == current_user["user_id"]).first()
        if not user:
            raise HTTPException(status_code=404, detail="使用者不存在")
        if not verify_password(data.old_password, user.password_hash):
            raise HTTPException(status_code=400, detail="舊密碼錯誤")
        user.password_hash = hash_password(data.new_password)
        user.must_change_password = False  # 使用者主動修改後清除強制旗標
        session.commit()
        return {"message": "密碼修改成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ============ Admin Routes ============

@router.get("/users")
def list_users(current_user: dict = Depends(require_permission(Permission.USER_MANAGEMENT_READ))):
    """列出所有使用者"""
    session = get_session()
    try:
        users = session.query(User, Employee).outerjoin(
            Employee, User.employee_id == Employee.id
        ).all()
        return [{
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "role_label": ROLE_LABELS.get(u.role, u.role),
            "permissions": u.permissions if u.permissions is not None else get_role_default_permissions(u.role),
            "is_active": u.is_active,
            "employee_id": u.employee_id,
            "employee_name": emp.name if emp else "",
            "last_login": u.last_login.isoformat() if u.last_login else None,
        } for u, emp in users]
    finally:
        session.close()


@router.post("/users", status_code=201)
def create_user(data: CreateUserRequest, current_user: dict = Depends(require_permission(Permission.USER_MANAGEMENT_WRITE))):
    """建立使用者帳號"""
    session = get_session()
    try:
        if session.query(User).filter(User.username == data.username).first():
            raise HTTPException(status_code=400, detail="帳號已存在")

        if data.employee_id is not None:
            if session.query(User).filter(User.employee_id == data.employee_id).first():
                raise HTTPException(status_code=400, detail="該員工已有帳號")
            emp = session.query(Employee).filter(Employee.id == data.employee_id).first()
            if not emp:
                raise HTTPException(status_code=404, detail="員工不存在")
        else:
            emp = None

        # 計算權限：若有指定則使用，否則套用角色預設
        if data.permissions is not None:
            final_permissions = data.permissions
        else:
            final_permissions = get_role_default_permissions(data.role)

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
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/users/{user_id}/reset-password")
def reset_password(user_id: int, data: ResetPasswordRequest, current_user: dict = Depends(require_permission(Permission.USER_MANAGEMENT_WRITE))):
    """重設密碼"""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="使用者不存在")
        user.password_hash = hash_password(data.new_password)
        user.must_change_password = True  # 管理員代為重設密碼，強制當事人下次登入修改
        user.token_version = (user.token_version or 0) + 1  # 使所有現有 session 的 token 立即無法刷新
        session.commit()
        return {"message": "密碼重設成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/permissions")
def get_permissions():
    """取得權限定義（供前端渲染 UI）"""
    return get_permissions_definition()


@router.put("/users/{user_id}")
def update_user(user_id: int, data: UpdateUserRequest, request: Request, current_user: dict = Depends(require_permission(Permission.USER_MANAGEMENT_WRITE))):
    """更新使用者角色與權限"""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="使用者不存在")

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
            (not user.is_active and old_is_active) or
            (user.role != old_role) or
            (user.permissions != old_permissions)
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
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/users/{user_id}")
def delete_user(user_id: int, current_user: dict = Depends(require_permission(Permission.USER_MANAGEMENT_WRITE))):
    """刪除使用者帳號"""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="使用者不存在")
        session.delete(user)
        session.commit()
        return {"message": "帳號已刪除"}
    finally:
        session.close()
