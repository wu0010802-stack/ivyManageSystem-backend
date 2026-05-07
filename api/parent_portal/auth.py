"""api/parent_portal/auth.py — 家長端 LIFF 登入與綁定

流程：
1. POST /api/parent/auth/liff-login
   家長前端從 LIFF SDK 取得 id_token → POST 至本端點。
   - LINE.user_id 已綁定家長 User → 發正式 access_token，回 ok
   - 未綁定 → 發 5 分鐘 temp_token（scope='bind'），回 need_binding
2. POST /api/parent/auth/bind
   未綁定家長以 temp_token + 行政發的綁定碼完成 claim：
   - atomic UPDATE guardian_binding_codes WHERE code_hash=? AND used_at IS NULL AND expires_at > now
   - 同 transaction 建 User(role='parent') + 設 Guardian.user_id
3. POST /api/parent/auth/bind-additional
   已綁定家長新增第二個小孩（共用 User）：
   - 需正式 access_token + role='parent'
   - atomic UPDATE 同上，但 Guardian.user_id 設為當前 user.id（不新建 User）
4. POST /api/parent/auth/logout
   清 cookie + token_version += 1（使所有 token 立即失效）

防護：
- IP rate-limit（共用 api/auth.py 既有 _check_ip_rate_limit）
- 帳號層失敗鎖（line_user_id 為 key，連 5 次失敗鎖 15 分鐘）
- atomic UPDATE 防 race（plan A.3）
- aud 校驗在 LineLoginService.verify_id_token 完成
"""

import hashlib
import logging
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from time import time as _time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response, Depends
from pydantic import BaseModel, Field
from sqlalchemy import update

from api.auth import (
    _check_ip_rate_limit,
    _ip_attempts as _login_ip_attempts,  # noqa: F401  (確保兩端 limiter 行為一致)
)
from models.database import (
    Guardian,
    GuardianBindingCode,
    ParentRefreshToken,
    User,
    get_session,
)
from services.line_login_service import LineLoginService
from utils.auth import (
    create_access_token,
    decode_token,
    get_current_user,
    require_parent_role,
    JWT_EXPIRE_MINUTES,
)
from utils.cookie import (
    clear_access_token_cookie,
    get_cookie_samesite,
    get_cookie_secure,
    set_access_token_cookie,
)

from ._shared import resolve_parent_display_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["parent-auth"])

# ── 模組層 LineLoginService 注入點（main.py 啟動時呼叫 init_parent_line_service） ──
_line_login_service: Optional[LineLoginService] = None


def init_line_login_service(service: LineLoginService) -> None:
    """注入 LineLoginService（main.py 啟動時呼叫一次）。"""
    global _line_login_service
    _line_login_service = service


def _get_line_login_service() -> LineLoginService:
    if _line_login_service is None:
        raise HTTPException(
            status_code=503,
            detail="LineLoginService 尚未注入（程式啟動順序錯誤）",
        )
    return _line_login_service


# ── bind token cookie / 帳號失敗鎖（line_user_id 層） ──────────────────────
_BIND_TOKEN_COOKIE = "parent_bind_token"
_BIND_TOKEN_PATH = "/api/parent/auth"
_BIND_TOKEN_TTL_MINUTES = 5

_BIND_FAIL_THRESHOLD = 5
_BIND_FAIL_LOCKOUT = 900  # 15 分鐘
_BIND_SCOPE = "parent_bind"

# In-process dict 仍保留，作為「DB 失敗時的 fail-open 配套」與測試 fixture
# reset target；正式擋線靠 DB-backed counter（multi-worker 安全）。
# Refs: 邏輯漏洞 audit 2026-05-07 P0 #14。
_bind_failures: dict[str, list[float]] = defaultdict(list)

# ── refresh token ────────────────────────────────────────────────────────
_REFRESH_COOKIE = "parent_refresh_token"
_REFRESH_COOKIE_PATH = "/api/parent/auth"
_REFRESH_TTL_DAYS = 30
_REFRESH_RACE_TOLERANCE_SECONDS = 5  # 同 token 雙請求 race 容忍窗


def _hash_refresh(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _gen_refresh_raw() -> str:
    # 384-bit 隨機，base64url 約 64 字
    return secrets.token_urlsafe(48)


def _set_refresh_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=raw,
        httponly=True,
        samesite=get_cookie_samesite(),
        secure=get_cookie_secure(),
        path=_REFRESH_COOKIE_PATH,
        max_age=_REFRESH_TTL_DAYS * 24 * 3600,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=_REFRESH_COOKIE,
        httponly=True,
        samesite=get_cookie_samesite(),
        secure=get_cookie_secure(),
        path=_REFRESH_COOKIE_PATH,
    )


def _issue_refresh_token(
    session,
    response: Response,
    *,
    user_id: int,
    family_id: str | None = None,
    parent_token_id: int | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> ParentRefreshToken:
    """寫一筆 ParentRefreshToken + 在 response 上 Set-Cookie。

    family_id=None → 視為新裝置，產生新 family_id。
    回傳 ORM 物件（caller 可進一步 commit / refresh）。
    """
    import uuid

    raw = _gen_refresh_raw()
    row = ParentRefreshToken(
        user_id=user_id,
        family_id=family_id or str(uuid.uuid4()),
        token_hash=_hash_refresh(raw),
        parent_token_id=parent_token_id,
        expires_at=_now() + timedelta(days=_REFRESH_TTL_DAYS),
        user_agent=(user_agent or "")[:255] or None,
        ip=(ip or "")[:45] or None,
    )
    session.add(row)
    session.flush()
    _set_refresh_cookie(response, raw)
    return row


def gc_expired_refresh_tokens(session, *, retention_days: int = 7) -> int:
    """刪除 token row：保留窗 retention_days 天供事後稽核，超出即刪。

    觸發條件（任一）：
    - `expires_at < cutoff`：自然過期已超出保留窗
    - `revoked_at < cutoff`：被 reuse 偵測或 logout 撤銷且超出保留窗
      （否則 reuse 攻擊頻繁時 table 會堆積到自然過期那刻才清）

    caller 負責 commit。
    """
    cutoff = _now() - timedelta(days=retention_days)
    result = session.execute(
        ParentRefreshToken.__table__.delete().where(
            (ParentRefreshToken.expires_at < cutoff)
            | (
                (ParentRefreshToken.revoked_at.isnot(None))
                & (ParentRefreshToken.revoked_at < cutoff)
            )
        )
    )
    return int(result.rowcount or 0)


def _set_bind_token_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_BIND_TOKEN_COOKIE,
        value=token,
        httponly=True,
        samesite=get_cookie_samesite(),
        secure=get_cookie_secure(),
        path=_BIND_TOKEN_PATH,
        max_age=_BIND_TOKEN_TTL_MINUTES * 60,
    )


def _clear_bind_token_cookie(response: Response) -> None:
    response.delete_cookie(
        key=_BIND_TOKEN_COOKIE,
        httponly=True,
        samesite=get_cookie_samesite(),
        secure=get_cookie_secure(),
        path=_BIND_TOKEN_PATH,
    )


def _check_bind_lockout(line_user_id: str) -> None:
    """LIFF 已驗證的 line_user_id 為單位做失敗鎖；累積 5 次失敗鎖 15 分鐘。

    走 DB-backed counter（rate_limit_buckets 表），multi-worker 一致。
    DB 失敗時 fail-open（utils/rate_limit_db.py 內部 log 警告）。
    """
    from utils.rate_limit_db import count_recent_attempts

    count = count_recent_attempts(
        _BIND_SCOPE, line_user_id, within_seconds=_BIND_FAIL_LOCKOUT
    )
    if count >= _BIND_FAIL_THRESHOLD:
        logger.warning(
            "家長綁定失敗次數過多，line_user_id=%s 已鎖 (failures=%d)",
            line_user_id,
            count,
        )
        raise HTTPException(
            status_code=429,
            detail="綁定失敗次數過多，請稍後再試",
        )


def _record_bind_failure(line_user_id: str) -> None:
    """記錄綁定失敗一次（DB-backed bucket）。"""
    from utils.rate_limit_db import record_attempt

    record_attempt(_BIND_SCOPE, line_user_id, window_seconds=_BIND_FAIL_LOCKOUT)


def _clear_bind_failures(line_user_id: str) -> None:
    """綁定成功後清除失敗計數（DB-backed bucket）。"""
    from utils.rate_limit_db import clear_attempts

    clear_attempts(_BIND_SCOPE, line_user_id)


# ── 內部工具 ────────────────────────────────────────────────────────────


def _hash_code(plain: str) -> str:
    return hashlib.sha256(plain.strip().encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now()


def _build_user_payload(user: User) -> dict:
    return {
        "user_id": user.id,
        "employee_id": None,
        "role": "parent",
        "name": user.username,
        "permissions": 0,
        "token_version": user.token_version or 0,
        "line_user_id": user.line_user_id,
    }


def _issue_access_token(response: Response, user: User) -> str:
    payload = _build_user_payload(user)
    token = create_access_token(payload)
    set_access_token_cookie(response, token)
    return token


def _issue_bind_temp_token(
    response: Response,
    line_user_id: str,
    display_name: Optional[str] = None,
) -> str:
    """5 分鐘 temp_token，scope='bind'。

    display_name 帶 LINE id_token payload['name']，供 /bind 建立 User 時直接寫入；
    temp_token 只能用來換綁定（不能讀其他 user 資料），夾帶 displayName 不增加風險。
    """
    payload = {
        "scope": "bind",
        "line_user_id": line_user_id,
    }
    if display_name:
        payload["display_name"] = display_name
    token = create_access_token(
        payload, expires_delta=timedelta(minutes=_BIND_TOKEN_TTL_MINUTES)
    )
    _set_bind_token_cookie(response, token)
    return token


def _decode_bind_temp_token(request: Request) -> tuple[str, Optional[str]]:
    """從 cookie 解 temp_token，回傳 (line_user_id, display_name)；無/過期/scope 不符 → 401。"""
    token = request.cookies.get(_BIND_TOKEN_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="未提供綁定臨時 Token")
    payload = decode_token(token)
    if payload.get("scope") != "bind":
        raise HTTPException(status_code=401, detail="Token scope 不符")
    line_user_id = payload.get("line_user_id")
    if not line_user_id:
        raise HTTPException(status_code=401, detail="Token 缺少 line_user_id")
    return line_user_id, payload.get("display_name")


def _claim_binding_code_atomic(
    session, code_hash: str, claimer_user_id: Optional[int]
) -> Optional[GuardianBindingCode]:
    """atomic UPDATE：把 guardian_binding_codes 標記為已用。

    僅當 used_at IS NULL 且 expires_at > now 時更新，rowcount=1 才視為成功。
    回傳更新後的 ORM 物件；失敗回 None（已用 / 過期 / 不存在）。

    claimer_user_id 在 bind-additional 流程是當前 user，liff bind 流程
    是新建的 user.id（兩階段流程：先 atomic UPDATE 鎖定碼、再建 User，
    最後再 update used_by_user_id）。
    """
    now = _now()
    stmt = (
        update(GuardianBindingCode)
        .where(
            GuardianBindingCode.code_hash == code_hash,
            GuardianBindingCode.used_at.is_(None),
            GuardianBindingCode.expires_at > now,
        )
        .values(used_at=now, used_by_user_id=claimer_user_id)
    )
    result = session.execute(stmt)
    if result.rowcount != 1:
        return None
    binding = (
        session.query(GuardianBindingCode)
        .filter(GuardianBindingCode.code_hash == code_hash)
        .first()
    )
    return binding


def _username_for_line(line_user_id: str) -> str:
    """家長 User 的 username 規則：parent_line_<完整 line_user_id>。

    LINE userId 為全球唯一的 33 字元字串（U 開頭），不會撞號；username
    欄位 String(50)，組合 ≤44 字元安全。
    """
    return f"parent_line_{line_user_id}"


def _create_parent_user(
    session, line_user_id: str, display_name: Optional[str] = None
) -> User:
    """建立 role='parent' User。password_hash 寫入永不匹配的 sentinel。

    display_name 為 LINE id_token payload['name']（LINE 個人檔案暱稱）；
    後續 home_summary / profile 等端點以此作為家長 hero 顯示名。
    """
    user = User(
        employee_id=None,
        username=_username_for_line(line_user_id),
        password_hash="!LINE_ONLY",  # sentinel，verify_password 永不通過
        role="parent",
        permissions=0,
        is_active=True,
        must_change_password=False,
        line_user_id=line_user_id,
        display_name=_clean_display_name(display_name),
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _clean_display_name(raw: Optional[str]) -> Optional[str]:
    """LINE displayName 可能含前後空白、過長或空字串，正規化後存入。

    - None / 全空白 → None
    - 截至 100 字元（與欄位上限對齊）
    """
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return cleaned[:100]


# ── Pydantic ────────────────────────────────────────────────────────────


class LiffLoginRequest(BaseModel):
    id_token: str = Field(..., min_length=10)


class BindRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=20)


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/liff-login")
def liff_login(
    payload: LiffLoginRequest,
    request: Request,
    response: Response,
):
    """LIFF 登入。家長前端先取得 id_token 再 POST。"""
    ip = request.client.host if request.client else "unknown"
    _check_ip_rate_limit(ip)

    service = _get_line_login_service()
    line_payload = service.verify_id_token(payload.id_token)
    line_user_id = line_payload["sub"]
    line_display_name = _clean_display_name(line_payload.get("name"))

    session = get_session()
    try:
        user = (
            session.query(User)
            .filter(User.line_user_id == line_user_id, User.is_active == True)
            .first()
        )
        if user and user.role == "parent":
            # 既有家長 user：若尚未寫過 display_name 或 LINE 暱稱有變則同步更新；
            # 不直接覆寫使用者已自定的名（目前無自定 UI，未來預留）
            if line_display_name and user.display_name != line_display_name:
                user.display_name = line_display_name
            _issue_access_token(response, user)
            _issue_refresh_token(
                session,
                response,
                user_id=user.id,
                user_agent=request.headers.get("user-agent"),
                ip=request.client.host if request.client else None,
            )
            user.last_login = _now()
            display_name = resolve_parent_display_name(session, user)
            session.commit()
            return {
                "status": "ok",
                "user": {
                    "user_id": user.id,
                    "name": display_name,
                    "role": "parent",
                },
            }

        # 沒有對應家長帳號：發臨時 token，引導去 bind
        _issue_bind_temp_token(response, line_user_id, line_display_name)
        return {
            "status": "need_binding",
            "line_user_id": line_user_id,
            "name_hint": line_display_name,
        }
    finally:
        session.close()


@router.post("/bind")
def bind_first_child(
    payload: BindRequest,
    request: Request,
    response: Response,
):
    """以綁定碼完成首次帳號綁定（建立 parent User 並掛 Guardian.user_id）。"""
    line_user_id, line_display_name = _decode_bind_temp_token(request)
    _check_bind_lockout(line_user_id)

    code_hash = _hash_code(payload.code)
    session = get_session()
    try:
        # 第一階段：先 atomic UPDATE 鎖定碼（claimer_user_id 暫填 NULL）
        binding = _claim_binding_code_atomic(session, code_hash, claimer_user_id=None)
        if binding is None:
            session.rollback()
            _record_bind_failure(line_user_id)
            raise HTTPException(status_code=400, detail="綁定碼無效、已使用或已過期")

        # 防同 LINE userId 重複建：若已有 parent User，僅補綁這筆 Guardian.user_id
        existing_user = (
            session.query(User)
            .filter(User.line_user_id == line_user_id, User.role == "parent")
            .first()
        )
        if existing_user:
            user = existing_user
            if line_display_name and user.display_name != line_display_name:
                user.display_name = line_display_name
        else:
            user = _create_parent_user(
                session, line_user_id, display_name=line_display_name
            )

        # 第二階段：把 used_by_user_id 落印 + 設 Guardian.user_id
        binding.used_by_user_id = user.id
        guardian = (
            session.query(Guardian).filter(Guardian.id == binding.guardian_id).first()
        )
        if guardian is None or guardian.deleted_at is not None:
            session.rollback()
            raise HTTPException(status_code=400, detail="此綁定碼對應的監護人已不存在")
        # ── 防綁定覆寫（F-001）─────────────────────────────────────────────
        # 若 Guardian 已被另一位 parent User 綁定，即使持碼者取得了仍未過期
        # 的綁定碼也不可覆寫，以阻擋「碼外洩 → 持外部 LINE 帳號搶綁 → 奪取
        # 他人 PII 並把原家長踢出」這條威脅鏈（對齊 bind-additional 既有
        # 守衛 idiom）。
        if guardian.user_id and guardian.user_id != user.id:
            logger.warning(
                "[parent-bind] 拒絕覆寫：guardian_id=%s 已被 user_id=%s 綁定，"
                "拒絕 line_user_id=%s（user_id=%s）的綁定請求；綁定碼可能外洩",
                guardian.id,
                guardian.user_id,
                line_user_id,
                user.id,
            )
            session.rollback()
            raise HTTPException(status_code=400, detail="此監護人已綁定其他家長帳號")
        guardian.user_id = user.id
        user.last_login = _now()
        # 同 LINE userId 已有 parent User 但又走首綁流程時（例：補綁失誤後重新拿
        # 首綁碼），舊裝置仍持有可旋轉的 refresh family。發新 family 前先撤銷舊 family，
        # 避免同帳號同時持兩條 rotation 鏈、logout 撤不乾淨。
        if existing_user is not None:
            session.query(ParentRefreshToken).filter(
                ParentRefreshToken.user_id == user.id,
                ParentRefreshToken.revoked_at.is_(None),
            ).update({"revoked_at": _now()}, synchronize_session=False)
        _issue_refresh_token(
            session,
            response,
            user_id=user.id,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        session.commit()
        session.refresh(user)

        _clear_bind_failures(line_user_id)
        _clear_bind_token_cookie(response)
        _issue_access_token(response, user)

        logger.warning(
            "[parent-bind] guardian_id=%s user_id=%s line_user_id=%s",
            guardian.id,
            user.id,
            line_user_id,
        )
        return {
            "status": "ok",
            "user": {
                "user_id": user.id,
                "name": resolve_parent_display_name(session, user),
                "role": "parent",
            },
        }
    finally:
        session.close()


@router.post("/bind-additional")
def bind_additional_child(
    payload: BindRequest,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
):
    """已登入家長以另一張綁定碼新增第二（含以上）個小孩。

    使用既有 access_token，**不**新建 User；只把對應 Guardian.user_id
    指向當前 user.id。
    """
    user_id = current_user["user_id"]
    code_hash = _hash_code(payload.code)
    session = get_session()
    try:
        binding = _claim_binding_code_atomic(
            session, code_hash, claimer_user_id=user_id
        )
        if binding is None:
            session.rollback()
            raise HTTPException(status_code=400, detail="綁定碼無效、已使用或已過期")
        guardian = (
            session.query(Guardian).filter(Guardian.id == binding.guardian_id).first()
        )
        if guardian is None or guardian.deleted_at is not None:
            session.rollback()
            raise HTTPException(status_code=400, detail="此綁定碼對應的監護人已不存在")
        if guardian.user_id and guardian.user_id != user_id:
            # 該 Guardian 已被別的 parent User 認領 → 即使取得了 code 也擋
            session.rollback()
            raise HTTPException(status_code=400, detail="此監護人已綁定其他家長帳號")
        guardian.user_id = user_id
        session.commit()

        logger.warning(
            "[parent-bind-additional] guardian_id=%s user_id=%s",
            guardian.id,
            user_id,
        )
        return {
            "status": "ok",
            "guardian_id": guardian.id,
            "student_id": guardian.student_id,
        }
    finally:
        session.close()


@router.post("/logout", status_code=204)
def parent_logout(
    request: Request,
    response: Response,
    current_user: dict = Depends(require_parent_role()),
):
    """登出：清 cookie + bump token_version + 撤銷當前 refresh family。"""
    user_id = current_user["user_id"]
    raw_refresh = request.cookies.get(_REFRESH_COOKIE)

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if user:
            user.token_version = (user.token_version or 0) + 1

        if raw_refresh:
            token_hash = _hash_refresh(raw_refresh)
            row = (
                session.query(ParentRefreshToken)
                .filter(ParentRefreshToken.token_hash == token_hash)
                .first()
            )
            if row is not None and row.revoked_at is None:
                session.query(ParentRefreshToken).filter(
                    ParentRefreshToken.family_id == row.family_id,
                    ParentRefreshToken.revoked_at.is_(None),
                ).update(
                    {"revoked_at": _now()},
                    synchronize_session=False,
                )

        session.commit()
    finally:
        session.close()

    clear_access_token_cookie(response)
    _clear_bind_token_cookie(response)
    _clear_refresh_cookie(response)
    return Response(status_code=204)


@router.post("/refresh")
def parent_refresh(request: Request, response: Response):
    """以家長 refresh token 換發新 access + 新 refresh（rotation）。

    狀態碼：
    - 200：rotation 成功
    - 401：cookie 缺、token 不存在、過期、family revoked、超出 race window 的 reuse
    - 409：5 秒內同 token 並發 race（前端應重打原請求）
    """
    raw = request.cookies.get(_REFRESH_COOKIE)
    if not raw:
        raise HTTPException(status_code=401, detail="未提供 refresh token")
    token_hash = _hash_refresh(raw)

    session = get_session()
    try:
        # postgres FOR UPDATE；sqlite no-op 但測試覆蓋夠
        row = (
            session.query(ParentRefreshToken)
            .filter(ParentRefreshToken.token_hash == token_hash)
            .with_for_update()
            .first()
        )
        if row is None:
            raise HTTPException(status_code=401, detail="refresh token 不存在")
        if row.revoked_at is not None:
            raise HTTPException(status_code=401, detail="refresh token 已撤銷")
        if row.expires_at < _now():
            raise HTTPException(status_code=401, detail="refresh token 已過期")

        if row.used_at is not None:
            # race window 容忍：5 秒內視為合法雙請求
            elapsed = (_now() - row.used_at).total_seconds()
            if 0 <= elapsed <= _REFRESH_RACE_TOLERANCE_SECONDS:
                logger.debug(
                    "[parent-refresh] race-tolerated user_id=%s family_id=%s elapsed=%.2fs",
                    row.user_id,
                    row.family_id,
                    elapsed,
                )
                # 不撤、不發新 token；前端應重打原請求
                raise HTTPException(
                    status_code=409, detail="rotation in progress, please retry"
                )
            # 超過 race window 仍拿 used token 來 refresh → reuse → 撤整個 family
            session.query(ParentRefreshToken).filter(
                ParentRefreshToken.family_id == row.family_id,
                ParentRefreshToken.revoked_at.is_(None),
            ).update(
                {"revoked_at": _now()},
                synchronize_session=False,
            )
            user = session.query(User).filter(User.id == row.user_id).first()
            if user is not None:
                user.token_version = (user.token_version or 0) + 1
            session.commit()
            logger.warning(
                "[parent-refresh] REUSE detected user_id=%s family_id=%s",
                row.user_id,
                row.family_id,
            )
            raise HTTPException(
                status_code=401, detail="refresh token 重用，整批已撤銷"
            )

        # 正常 rotation
        user = (
            session.query(User)
            .filter(User.id == row.user_id, User.is_active == True)  # noqa: E712
            .first()
        )
        if user is None:
            raise HTTPException(status_code=401, detail="使用者已停用")

        row.used_at = _now()
        new_row = _issue_refresh_token(
            session,
            response,
            user_id=user.id,
            family_id=row.family_id,
            parent_token_id=row.id,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
        user.last_login = _now()
        _issue_access_token(response, user)
        display_name = resolve_parent_display_name(session, user)
        session.commit()

        return {
            "status": "ok",
            "user": {
                "user_id": user.id,
                "name": display_name,
                "role": "parent",
            },
        }
    finally:
        session.close()
