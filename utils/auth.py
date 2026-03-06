"""
Authentication utilities - password hashing and JWT tokens
"""

import hmac
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
JWT_EXPIRE_MINUTES = 15          # Access token 有效期（分鐘）；短期 token 將帳號停用後的暴露窗口從 24h 縮至最長 15min
JWT_REFRESH_GRACE_HOURS = 24  # 過期後仍允許刷新的寬限時間（24 小時）；帳號停用後暴露窗口從 7 天縮至 1 天

# ── 密碼雜湊參數 ───────────────────────────────────────────────────────────
# OWASP 2023 建議：PBKDF2-HMAC-SHA256 至少 600,000 次迭代
PBKDF2_ITERATIONS = 600_000
_LEGACY_ITERATIONS = 100_000  # 舊版雜湊，僅用於向下相容驗證

# 儲存格式（新）：{iterations}${salt_hex}${hash_hex}
# 儲存格式（舊）：{salt_hex}${hash_hex}  ← 固定 100,000 次
# 識別方式：split("$") 得到 3 段 → 新格式；2 段 → 舊格式
# ─────────────────────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """使用 PBKDF2-HMAC-SHA256 雜湊密碼，格式：{iterations}${salt}${hash}"""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"{PBKDF2_ITERATIONS}${salt}${h.hex()}"


def _dummy_hash(plain_password: str) -> None:
    """執行一次與正常驗證等量的 PBKDF2，用於格式無效時維持恆定回應時間。
    防止攻擊者透過回應時間差探測 hash 格式是否合法（Timing Side-Channel）。
    """
    hashlib.pbkdf2_hmac("sha256", plain_password.encode(), b"__dummy__", PBKDF2_ITERATIONS)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """驗證密碼，同時相容新格式（含 iterations）與舊格式（固定 100,000 次）。
    使用 hmac.compare_digest 進行恆定時間比對，防止 Timing Attack。
    格式無效或解析失敗時執行 dummy hash，確保回應時間一致。
    """
    try:
        parts = hashed_password.split("$", 2)
        if len(parts) == 3:
            # 新格式：iterations$salt$hash
            iterations = int(parts[0])
            salt, stored_hash = parts[1], parts[2]
        elif len(parts) == 2:
            # 舊格式：salt$hash（固定 100,000 次）
            iterations = _LEGACY_ITERATIONS
            salt, stored_hash = parts[0], parts[1]
        else:
            # 格式不合法：仍執行 dummy hash，避免即時回傳洩漏格式資訊
            _dummy_hash(plain_password)
            return False
        h = hashlib.pbkdf2_hmac("sha256", plain_password.encode(), salt.encode(), iterations)
        return hmac.compare_digest(h.hex(), stored_hash)
    except (ValueError, AttributeError):
        # 解析失敗（iterations 非整數、None 等）：同樣執行 dummy hash
        _dummy_hash(plain_password)
        return False


def needs_rehash(hashed_password: str) -> bool:
    """判斷密碼雜湊是否需要以目前的參數重新雜湊（舊格式或低迭代次數）。"""
    parts = hashed_password.split("$", 2)
    if len(parts) == 3:
        try:
            return int(parts[0]) < PBKDF2_ITERATIONS
        except ValueError:
            return True  # 格式損毀，應重新雜湊
    return True  # 舊格式，需升級


def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=JWT_EXPIRE_MINUTES))
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
