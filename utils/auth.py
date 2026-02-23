"""
Authentication utilities - password hashing and JWT tokens
"""

import os
import logging
import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, Header, HTTPException
from jose import JWTError, jwt

logger = logging.getLogger(__name__)

_jwt_secret = os.environ.get("JWT_SECRET_KEY", "")
_is_dev = os.environ.get("ENV", "development").lower() in ("development", "dev", "local")

if not _jwt_secret:
    if _is_dev:
        _jwt_secret = "dev-only-insecure-key-do-not-use-in-production"
        logger.warning("JWT_SECRET_KEY 未設定，使用開發用預設值。請勿在正式環境使用！")
    else:
        raise RuntimeError("JWT_SECRET_KEY 環境變數未設定，正式環境不允許啟動。")

JWT_SECRET_KEY = _jwt_secret
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24
JWT_REFRESH_GRACE_HOURS = 72  # 過期後仍允許刷新的寬限時間


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${h.hex()}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        salt, stored_hash = hashed_password.split("$", 1)
        h = hashlib.pbkdf2_hmac("sha256", plain_password.encode(), salt.encode(), 100_000)
        return h.hex() == stored_hash
    except (ValueError, AttributeError):
        return False


def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=JWT_EXPIRE_HOURS))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="無效或過期的 Token")


def decode_token_allow_expired(token: str) -> dict:
    """解碼 token，允許在寬限期內的過期 token（用於 refresh）。
    回傳 payload，若 token 無效或超出寬限期則拋出 401。
    """
    try:
        # 先嘗試正常解碼（未過期）
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        # Token 已過期，跳過 exp 驗證取出 payload
        payload = jwt.decode(
            token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM],
            options={"verify_exp": False},
        )
        # 檢查是否在寬限期內
        exp = payload.get("exp", 0)
        now = datetime.utcnow().timestamp()
        grace_seconds = JWT_REFRESH_GRACE_HOURS * 3600
        if now - exp > grace_seconds:
            raise HTTPException(status_code=401, detail="Token 已超過可刷新期限，請重新登入")
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="無效的 Token，請重新登入")


async def get_current_user(authorization: str = Header(None)):
    """FastAPI dependency: extract and verify JWT from Authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未提供認證 Token")
    token = authorization.split(" ", 1)[1]
    payload = decode_token(token)
    employee_id = payload.get("employee_id")
    if employee_id is None:
        raise HTTPException(status_code=401, detail="Token 資料不完整")
    return payload


async def require_admin(current_user: dict = Depends(get_current_user)):
    """FastAPI dependency: require admin role."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="僅限管理員操作")
    return current_user


def require_permission(permission):
    """
    FastAPI dependency factory: require specific permission.

    Usage:
        @router.get("/some-route")
        def some_endpoint(current_user: dict = Depends(require_permission(Permission.EMPLOYEES))):
            ...
    """
    from utils.permissions import has_permission

    async def check_permission(current_user: dict = Depends(get_current_user)):
        user_permissions = current_user.get("permissions", 0)
        if not has_permission(user_permissions, permission):
            raise HTTPException(status_code=403, detail="您沒有此功能的存取權限")
        return current_user

    return check_permission
