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
    hash_password, verify_password, create_access_token, get_current_user,
    decode_token_allow_expired,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ---------- Login Rate Limiter ----------
# 每個 IP / 帳號 在 WINDOW 秒內最多 MAX_ATTEMPTS 次嘗試
_LOGIN_WINDOW = 300  # 5 分鐘
_LOGIN_MAX_ATTEMPTS = 10
_login_attempts: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(key: str):
    """檢查登入頻率，超出則拋 429"""
    now = time.time()
    attempts = _login_attempts[key]
    # 清除過期紀錄
    _login_attempts[key] = [t for t in attempts if now - t < _LOGIN_WINDOW]
    if len(_login_attempts[key]) >= _LOGIN_MAX_ATTEMPTS:
        logger.warning(f"登入頻率超限: {key}")
        raise HTTPException(status_code=429, detail="登入嘗試次數過多，請稍後再試")
    _login_attempts[key].append(now)


# ============ Pydantic Models ============

class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    employee_id: int
    username: str
    password: str
    role: str = "teacher"


class ResetPasswordRequest(BaseModel):
    new_password: str


class ImpersonateRequest(BaseModel):
    employee_id: int


# ============ Public Routes ============

@router.post("/impersonate")
def impersonate_user(data: ImpersonateRequest, current_user: dict = Depends(get_current_user)):
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
        
        # 3. 如果沒有使用者帳號，我們先拒絕（或者可以動態建立一個臨時 token context，但這樣比較複雜）
        # 目前假設只能切換到有帳號的員工（通常是老師）
        if not target_user:
            raise HTTPException(status_code=400, detail="該員工沒有使用者帳號，無法切換")
            
        # 4. 產生該使用者的 token
        token = create_access_token({
            "user_id": target_user.id,
            "employee_id": target_user.employee_id,
            "role": target_user.role,
            "name": target_emp.name,
        })
        
        return {
            "token": token,
            "user": {
                "id": target_user.id,
                "username": target_user.username,
                "role": target_user.role,
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
    _check_rate_limit(f"ip:{client_ip}")
    _check_rate_limit(f"user:{data.username}")

    session = get_session()
    try:
        user = session.query(User).filter(
            User.username == data.username,
            User.is_active == True,
        ).first()

        if not user or not verify_password(data.password, user.password_hash):
            raise HTTPException(status_code=401, detail="帳號或密碼錯誤")

        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()

        user.last_login = datetime.now()
        session.commit()

        token = create_access_token({
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": emp.name if emp else "",
        })

        return {
            "token": token,
            "user": {
                "id": user.id,
                "username": user.username,
                "role": user.role,
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

        emp = session.query(Employee).filter(Employee.id == user.employee_id).first()

        new_token = create_access_token({
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": emp.name if emp else "",
        })

        return {
            "token": new_token,
            "user": {
                "id": user.id,
                "username": user.username,
                "role": user.role,
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
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
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
def list_users(current_user: dict = Depends(get_current_user)):
    """列出所有使用者（管理員限定）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="權限不足")
    session = get_session()
    try:
        users = session.query(User, Employee).outerjoin(
            Employee, User.employee_id == Employee.id
        ).all()
        return [{
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "is_active": u.is_active,
            "employee_id": u.employee_id,
            "employee_name": emp.name if emp else "",
            "last_login": u.last_login.isoformat() if u.last_login else None,
        } for u, emp in users]
    finally:
        session.close()


@router.post("/users")
def create_user(data: CreateUserRequest, current_user: dict = Depends(get_current_user)):
    """建立使用者帳號（管理員限定）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="權限不足")
    session = get_session()
    try:
        if session.query(User).filter(User.username == data.username).first():
            raise HTTPException(status_code=400, detail="帳號已存在")
        if session.query(User).filter(User.employee_id == data.employee_id).first():
            raise HTTPException(status_code=400, detail="該員工已有帳號")
        emp = session.query(Employee).filter(Employee.id == data.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        user = User(
            employee_id=data.employee_id,
            username=data.username,
            password_hash=hash_password(data.password),
            role=data.role,
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
def reset_password(user_id: int, data: ResetPasswordRequest, current_user: dict = Depends(get_current_user)):
    """重設密碼（管理員限定）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="權限不足")
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="使用者不存在")
        user.password_hash = hash_password(data.new_password)
        session.commit()
        return {"message": "密碼重設成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/users/{user_id}")
def delete_user(user_id: int, current_user: dict = Depends(get_current_user)):
    """刪除使用者帳號（管理員限定）"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="權限不足")
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
