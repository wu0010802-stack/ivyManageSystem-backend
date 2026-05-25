"""
Authentication utilities - password hashing and JWT tokens
"""

import hmac
import logging
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt

from config import settings

logger = logging.getLogger(__name__)

_jwt_secret = settings.core.jwt_secret_key or ""
_is_dev = settings.core.env.lower() in ("development", "dev", "local")

if not _jwt_secret:
    if _is_dev:
        _jwt_secret = secrets.token_urlsafe(64)
        logger.warning(
            "JWT_SECRET_KEY 未設定，已隨機產生開發用 secret；每次重啟會 invalidate 所有 session。請勿在正式環境使用！"
        )
    else:
        raise RuntimeError("JWT_SECRET_KEY 環境變數未設定，正式環境不允許啟動。")

JWT_SECRET_KEY = _jwt_secret


# ── Multi-key support for rotation ────────────────────────────────────────
# 設計：docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md
# JWT_SECRET_KEY 為 current（簽 + 驗第一順位）；
# JWT_SECRET_KEYS_OLDS 為 JSON list of accept-only secrets，rotation 過渡期用。
import json as _json


def _kid_for(secret: str) -> str:
    """kid = sha256(secret) 前 12 hex chars。確定性、不洩漏 secret。"""
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


_olds_raw = settings.core.jwt_secret_keys_olds
try:
    _olds = _json.loads(_olds_raw)
    if not isinstance(_olds, list) or not all(isinstance(k, str) for k in _olds):
        raise ValueError("JWT_SECRET_KEYS_OLDS 必須是 JSON list of strings")
except (_json.JSONDecodeError, ValueError) as _e:
    if _is_dev:
        logger.warning("JWT_SECRET_KEYS_OLDS 解析失敗，視為空 list：%s", _e)
        _olds = []
    else:
        raise RuntimeError(f"JWT_SECRET_KEYS_OLDS 解析失敗：{_e}")

_CURRENT_KID: str = _kid_for(JWT_SECRET_KEY)
# verify 查表：kid → secret。current 永遠在內；olds 接著加。
_VERIFY_KEYS: dict[str, str] = {_CURRENT_KID: JWT_SECRET_KEY}
for _old in _olds:
    if not _old:
        continue
    _VERIFY_KEYS[_kid_for(_old)] = _old

# 過渡期：沒帶 kid header 的 legacy token，依序試這個 list。
_LEGACY_TRY_ORDER: list[str] = [JWT_SECRET_KEY] + [k for k in _olds if k]

# Runbook 用：rotation 操作者看 log 確認新 env 已被載入。kid 是 sha256 截短不洩漏 secret。
logger.info(
    "JWT _VERIFY_KEYS 載入 %d 個 kid：%s（current=%s）",
    len(_VERIFY_KEYS),
    list(_VERIFY_KEYS.keys()),
    _CURRENT_KID,
)
# ──────────────────────────────────────────────────────────────────────────

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 15  # Access token 有效期（分鐘）；短期 token 將帳號停用後的暴露窗口從 24h 縮至最長 15min
JWT_REFRESH_GRACE_HOURS = 2  # 過期後仍允許刷新的寬限時間（2 小時）；搭配 token_version 機制，帳號停用後舊 token 立即失效
# S2: staff 端 absolute session lifetime。
# 從首次登入算起，整段 session（含多次 refresh）不得超過此小時數。
# 超過時 /refresh 拒絕，需重新登入。
# Why: 原本只要 refresh 不過 grace（2h），偷 cookie 可永久 /refresh 延期；
# 家長端已有 ParentRefreshToken family rotation，staff 端用較簡化的
# absolute lifetime 起步，先把「永續延期」窗口封死。
JWT_ABSOLUTE_LIFETIME_HOURS = settings.core.jwt_absolute_lifetime_hours
_PASSWORD_CHANGE_ALLOWED_PATHS = {
    "/api/auth/change-password",
    "/api/auth/logout",
}

# ── 密碼雜湊參數 ───────────────────────────────────────────────────────────
# OWASP 2023 建議：PBKDF2-HMAC-SHA256 至少 600,000 次迭代
PBKDF2_ITERATIONS = 600_000
_LEGACY_ITERATIONS = 100_000  # 舊版雜湊，僅用於向下相容驗證

# 儲存格式（新）：{iterations}${salt_hex}${hash_hex}
# 儲存格式（舊）：{salt_hex}${hash_hex}  ← 固定 100,000 次
# 識別方式：split("$") 得到 3 段 → 新格式；2 段 → 舊格式
# ─────────────────────────────────────────────────────────────────────────

import re

# ── 密碼強度規則 ─────────────────────────────────────────────────────────
_PASSWORD_MIN_LENGTH = 8


def validate_password_strength(password: str) -> None:
    """驗證密碼強度，不合規則時拋出 400 HTTPException。

    規則：
    - 至少 8 字元
    - 至少包含一個大寫字母
    - 至少包含一個小寫字母
    - 至少包含一個數字
    """
    errors = []
    if len(password) < _PASSWORD_MIN_LENGTH:
        errors.append(f"至少 {_PASSWORD_MIN_LENGTH} 個字元")
    if not re.search(r"[A-Z]", password):
        errors.append("至少一個大寫英文字母")
    if not re.search(r"[a-z]", password):
        errors.append("至少一個小寫英文字母")
    if not re.search(r"\d", password):
        errors.append("至少一個數字")
    if errors:
        raise HTTPException(
            status_code=400,
            detail=f"密碼強度不足：{', '.join(errors)}",
        )


def hash_password(password: str) -> str:
    """使用 PBKDF2-HMAC-SHA256 雜湊密碼，格式：{iterations}${salt}${hash}"""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS
    )
    return f"{PBKDF2_ITERATIONS}${salt}${h.hex()}"


def _dummy_hash(plain_password: str) -> None:
    """執行一次與正常驗證等量的 PBKDF2，用於格式無效時維持恆定回應時間。
    防止攻擊者透過回應時間差探測 hash 格式是否合法（Timing Side-Channel）。
    """
    hashlib.pbkdf2_hmac(
        "sha256", plain_password.encode(), b"__dummy__", PBKDF2_ITERATIONS
    )


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
        h = hashlib.pbkdf2_hmac(
            "sha256", plain_password.encode(), salt.encode(), iterations
        )
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
    """簽發 access token；自動產生 jti 以支援 LOW-2 黑名單機制。

    呼叫端可以自行傳 `jti` 覆寫（測試用）；多數情境讓本函數產生即可。

    iat / original_iat：
    - `iat` 為本次 token 簽發時間（每次 refresh 重置），unix epoch 秒。
    - `original_iat` 為「整段登入 session」最早的 iat，refresh 時呼叫端必須
      把舊 token 的 original_iat 帶進來，本函數會保留；首次登入時呼叫端
      不傳，本函數會用 iat 預填。S2 absolute lifetime 用此欄位判斷
      session 總長是否已超出強制重登門檻。
    """
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(minutes=JWT_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    iat_ts = int(now.timestamp())
    to_encode.setdefault("iat", iat_ts)
    to_encode.setdefault("original_iat", iat_ts)
    to_encode.setdefault("jti", secrets.token_urlsafe(16))
    return jwt.encode(
        to_encode,
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
        headers={"kid": _CURRENT_KID},
    )


def is_token_revoked(jti: str) -> bool:
    """LOW-2：查詢 jti 是否已加入 blocklist。

    DB 失敗時 fail-open（log 警告，但不拒絕請求）—— 若 DB 抖動就把全站打掛。
    """
    if not jti:
        return False
    try:
        from sqlalchemy import text

        from models.base import get_engine

        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT 1 FROM jwt_blocklist WHERE jti = :jti AND expires_at > :now"
                ),
                {"jti": jti, "now": datetime.now(timezone.utc)},
            ).fetchone()
            return row is not None
    except Exception as e:
        logger.warning("is_token_revoked 查詢失敗，fail-open: %s", e)
        return False


def revoke_token(
    jti: str,
    expires_at: datetime,
    reason: str = "logout",
) -> None:
    """LOW-2：把 jti 寫入 blocklist。

    - jti 為空或已存在 → 靜默忽略（idempotent）
    - DB 失敗 → log error 但不拋（不阻擋登出流程）
    """
    if not jti:
        return
    try:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        from models.base import get_engine

        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO jwt_blocklist (jti, expires_at, revoked_at, reason)
                    VALUES (:jti, :expires_at, :revoked_at, :reason)
                    ON CONFLICT (jti) DO NOTHING
                    """),
                {
                    "jti": jti,
                    "expires_at": expires_at,
                    "revoked_at": datetime.now(timezone.utc),
                    "reason": reason,
                },
            )
    except Exception as e:
        logger.error("revoke_token 寫入失敗 jti=%s: %s", jti, e)


def cleanup_jwt_blocklist() -> int:
    """LOW-2：刪除已過期的黑名單項。回傳刪除筆數。

    通常每天呼叫一次（由 scheduler 排程）。
    """
    try:
        from sqlalchemy import text

        from models.base import get_engine

        engine = get_engine()
        with engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM jwt_blocklist WHERE expires_at < :now"),
                {"now": datetime.now(timezone.utc)},
            )
            return result.rowcount or 0
    except Exception as e:
        logger.warning("cleanup_jwt_blocklist 失敗: %s", e)
        return 0


def _check_token_algorithm(token: str) -> None:
    """解碼前顯式驗證 JWT header 的 alg 欄位。

    Defense-in-depth：即使 python-jose 因升版 regression 而不再過濾非白名單算法，
    此函式仍能獨立攔截 alg:none、RS256 等算法混淆攻擊（RFC 7518 §3.6）。
    必須在呼叫 jwt.decode() 之前執行。

    Raises:
        HTTPException(401)：alg 欄位不存在、為空、或不等於 JWT_ALGORITHM。
    """
    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="無效或過期的 Token")
    if header.get("alg") != JWT_ALGORITHM:
        raise HTTPException(status_code=401, detail="無效或過期的 Token")


def _decode_with_keys(token: str, *, allow_expired: bool = False) -> dict:
    """Multi-key 容忍的 JWT decode。已先過 _check_token_algorithm。

    解析順序：
    1. 有 kid header → 用 _VERIFY_KEYS[kid]（未知 kid → 401）
    2. 無 kid（過渡期 legacy token）→ 依序試 _LEGACY_TRY_ORDER
    3. 都失敗 → 401
    """
    options = {"verify_exp": False} if allow_expired else {}

    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="無效或過期的 Token")

    kid = header.get("kid")
    if kid:
        secret = _VERIFY_KEYS.get(kid)
        if not secret:
            raise HTTPException(status_code=401, detail="無效或過期的 Token")
        try:
            return jwt.decode(
                token, secret, algorithms=[JWT_ALGORITHM], options=options
            )
        except JWTError:
            raise HTTPException(status_code=401, detail="無效或過期的 Token")

    # legacy（無 kid）：依序試 _LEGACY_TRY_ORDER
    for secret in _LEGACY_TRY_ORDER:
        try:
            return jwt.decode(
                token, secret, algorithms=[JWT_ALGORITHM], options=options
            )
        except JWTError:
            continue
    raise HTTPException(status_code=401, detail="無效或過期的 Token")


def decode_token(token: str) -> dict:
    _check_token_algorithm(token)
    return _decode_with_keys(token, allow_expired=False)


def verify_ws_token(token: str) -> dict:
    """驗證 WebSocket 連線的 JWT token，並確認帳號當前狀態有效。

    在 decode_token（簽名 + 過期驗證）的基礎上，額外查詢 DB 確認：
    - is_active：帳號未被停用
    - token_version：舊 token 在密碼變更 / 強制登出後立即失效
    - must_change_password：尚未更改預設密碼的帳號禁止建立 WS 連線

    payload 不含 user_id（無綁定帳號的 guest token）時略過 DB 查詢。

    Raises:
        HTTPException(401)：token 無效、帳號停用、token_version 不符
        HTTPException(403)：帳號需先修改密碼
    """
    payload = decode_token(token)  # 驗證 JWT 簽名與過期
    if is_token_revoked(payload.get("jti", "")):
        raise HTTPException(status_code=401, detail="Token 已廢止，請重新登入")

    user_id = payload.get("user_id")
    if user_id is None:
        return payload  # 無帳號綁定，略過 DB 查詢

    from models.database import get_session, User

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="使用者已停用或不存在")
        if payload.get("token_version", 0) != (user.token_version or 0):
            raise HTTPException(
                status_code=401, detail="Token 已失效，請重新登入（帳號狀態已變更）"
            )
        if user.must_change_password:
            raise HTTPException(status_code=403, detail="需先修改密碼後才能使用系統")
    finally:
        session.close()

    return payload


def decode_token_allow_expired(token: str) -> dict:
    """解碼 token，允許在寬限期內的過期 token（用於 refresh / end-impersonate / logout）。
    回傳 payload，若 token 無效、超出寬限期、或 jti 已被廢止則拋出 401。
    """
    _check_token_algorithm(token)
    # 一律 allow_expired=True 解，再手動檢 exp + grace。簽章錯誤仍會 raise 401。
    payload = _decode_with_keys(token, allow_expired=True)
    exp = payload.get("exp", 0)
    now = datetime.now(timezone.utc).timestamp()
    grace_seconds = JWT_REFRESH_GRACE_HOURS * 3600
    if now - exp > grace_seconds:
        raise HTTPException(
            status_code=401, detail="Token 已超過可刷新期限，請重新登入"
        )
    # JTI 廢止檢查：與 decode_token 對齊。logout 廢止後的 token 在寬限期內仍可解碼，
    # 必須額外擋住才能讓 /refresh / /end-impersonate 不被遺失的 cookie 反覆利用。
    if is_token_revoked(payload.get("jti", "")):
        raise HTTPException(status_code=401, detail="Token 已廢止，請重新登入")
    return payload


def decode_token_for_audit(token: str | None) -> dict | None:
    """專供 audit 路徑使用的 decode：multi-key 容忍、verify_exp=False、不檢 jti / token_version。

    純粹從 token 抽 user_id / name 寫 audit log。失敗一律回 None，**不** 拋例外。

    安全性：本函式僅供 audit log 寫入路徑使用，**不可** 用於授權判斷。
    呼叫端不得用回傳值通過 require_permission / get_current_user 等守衛。
    """
    if not token:
        return None
    try:
        _check_token_algorithm(token)
        return _decode_with_keys(token, allow_expired=True)
    except (JWTError, HTTPException):
        return None


async def get_current_user(request: Request):
    """FastAPI dependency: extract and verify JWT from httpOnly Cookie or Authorization header.

    優先順序：
    1. Cookie 'access_token'（httpOnly，XSS 無法讀取）
    2. Authorization: Bearer ... header（向下相容 / Swagger UI）
    """
    # 1. httpOnly Cookie（主要路徑）
    token = request.cookies.get("access_token")
    # 2. Fallback: Authorization header
    if not token:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1]
    if not token:
        raise HTTPException(status_code=401, detail="未提供認證 Token")
    payload = decode_token(token)
    if is_token_revoked(payload.get("jti", "")):
        raise HTTPException(status_code=401, detail="Token 已廢止，請重新登入")
    # scope-restricted token（例如 parent_portal bind temp token，scope='bind'）
    # 必須由各自的 _decode_*_token 驗證並僅限其專屬端點，禁止經由此通用
    # dependency 進入後台/教師端 router（否則會繞過 require_non_parent_role 的
    # 黑名單守衛，洩漏例如 /api/portal/calendar 的內部會議資料）。
    if payload.get("scope") is not None:
        raise HTTPException(
            status_code=401,
            detail="此 token 為受限用途，不可用於資源存取",
        )
    user_id = payload.get("user_id")
    if user_id is None:
        return payload

    from models.database import get_session, User

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="使用者已停用或不存在")
        if payload.get("token_version", 0) != (user.token_version or 0):
            raise HTTPException(
                status_code=401, detail="Token 已失效，請重新登入（帳號狀態已變更）"
            )
        if (
            user.must_change_password
            and request.url.path not in _PASSWORD_CHANGE_ALLOWED_PATHS
        ):
            raise HTTPException(status_code=403, detail="需先修改密碼後才能使用系統")
        payload["must_change_password"] = bool(user.must_change_password)
        # JWT 原本不含 username；補進 payload，讓全站稽核欄位（operator / reviewed_by 等）
        # 能真正記錄到是誰操作，而不是長期為空字串
        payload["username"] = user.username
    finally:
        session.close()
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


def require_staff_permission(permission):
    """限制管理端 API 僅供 admin/hr/supervisor 角色使用，並保留既有 permission 檢查。

    教師（teacher）走 portal 自助介面、家長（parent）走家長入口；兩者皆不得直接撞管理端 API。
    """

    async def check_staff_permission(
        current_user: dict = Depends(require_permission(permission)),
    ):
        role = current_user.get("role")
        if role == "teacher":
            raise HTTPException(
                status_code=403, detail="教師帳號不可直接存取管理端 API"
            )
        if role == "parent":
            raise HTTPException(
                status_code=403, detail="家長帳號不可直接存取管理端 API"
            )
        return current_user

    return check_staff_permission


def require_parent_role():
    """FastAPI dependency factory：限制端點僅供家長 (role='parent') 使用。"""

    async def check(current_user: dict = Depends(get_current_user)):
        if current_user.get("role") != "parent":
            raise HTTPException(status_code=403, detail="此 API 僅限家長端使用")
        return current_user

    return check


def require_non_parent_role():
    """FastAPI dependency factory：拒絕家長 token 撞員工/管理端路由。

    Portal 與所有非家長路由建議在 router 層加掛此 dependency，
    把保證從「每個 endpoint 都記得呼叫 _get_employee」升級為結構性擋線。
    """

    async def check(current_user: dict = Depends(get_current_user)):
        if current_user.get("role") == "parent":
            raise HTTPException(status_code=403, detail="家長帳號不可存取此 API")
        return current_user

    return check
