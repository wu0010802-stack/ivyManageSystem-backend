"""services/staff_refresh.py — 員工端 refresh token rotation helpers.

1:1 port from api/parent_portal/auth.py rotation logic.

重要：rotate_refresh_token 在 reuse 分支需 commit BEFORE raising HTTPException，
故使用 get_session()/try/finally 而非 session_scope()（session_scope 的 except
路徑會 rollback，導致 family revoke + token_version bump 遺失）。

Spec F (audit P1 #11)
"""

import logging
import secrets
import uuid
from datetime import timedelta
from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy import func

from models.auth import User
from models.base import get_session, session_scope
from models.staff_refresh_token import StaffRefreshToken
from utils.taipei_time import now_taipei_naive

logger = logging.getLogger(__name__)

# 與 parent 對齊
REFRESH_LIFETIME = timedelta(days=30)
RACE_TOLERANCE_SECONDS = 5


def _hash_token(raw: str) -> str:
    return sha256(raw.encode("utf-8")).hexdigest()


def issue_refresh_token(
    user_id: int,
    family_id: str | None = None,
    parent_token_id: int | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[str, int]:
    """生成 refresh token 並寫 DB。回傳 (raw, db_id)。"""
    raw = secrets.token_urlsafe(48)
    token_hash = _hash_token(raw)
    expires_at = now_taipei_naive() + REFRESH_LIFETIME
    fam = family_id or str(uuid.uuid4())

    with session_scope() as session:
        rt = StaffRefreshToken(
            user_id=user_id,
            family_id=fam,
            token_hash=token_hash,
            parent_token_id=parent_token_id,
            expires_at=expires_at,
            user_agent=(user_agent or "")[:255] or None,
            ip=(ip or "")[:45] or None,
        )
        session.add(rt)
        session.flush()
        db_id = rt.id
    return raw, db_id


def rotate_refresh_token(
    raw_token: str, user_agent: str | None, ip: str | None
) -> tuple[str, int]:
    """Verify + rotate. Return (new_raw_token, user_id). Raise 401/409 on error.

    使用 get_session()/try/finally（非 session_scope），因為 reuse 分支需要
    在 raise HTTPException 前先 commit（session_scope 的 except 路徑會 rollback）。
    """
    token_hash = _hash_token(raw_token)

    session = get_session()
    try:
        # postgres FOR UPDATE；SQLite no-op 但測試覆蓋夠
        rt = (
            session.query(StaffRefreshToken)
            .filter(StaffRefreshToken.token_hash == token_hash)
            .with_for_update()
            .first()
        )

        if rt is None:
            raise HTTPException(status_code=401, detail="refresh token 不存在")

        if rt.revoked_at is not None:
            raise HTTPException(status_code=401, detail="refresh token 已撤銷")

        if rt.expires_at < now_taipei_naive():
            raise HTTPException(status_code=401, detail="refresh token 已過期")

        # Reuse detection
        if rt.used_at is not None:
            elapsed = (now_taipei_naive() - rt.used_at).total_seconds()
            if 0 <= elapsed <= RACE_TOLERANCE_SECONDS:
                # Race window：5 秒內同 token 雙請求，前端應重打原請求
                logger.debug(
                    "[staff-refresh] race-tolerated user_id=%s family_id=%s elapsed=%.2fs",
                    rt.user_id,
                    rt.family_id,
                    elapsed,
                )
                raise HTTPException(
                    status_code=409, detail="rotation in progress, please retry"
                )
            # 超過 race window → reuse → 撤整 family + bump token_version
            session.query(StaffRefreshToken).filter(
                StaffRefreshToken.family_id == rt.family_id,
                StaffRefreshToken.revoked_at.is_(None),
            ).update(
                {"revoked_at": now_taipei_naive()},
                synchronize_session=False,
            )
            user = session.query(User).filter(User.id == rt.user_id).first()
            if user is not None:
                user.token_version = (user.token_version or 0) + 1
            # commit BEFORE raise（session_scope 的 except 路徑會 rollback，此處必須先 commit）
            session.commit()
            logger.warning(
                "[staff-refresh] REUSE detected user_id=%s family_id=%s",
                rt.user_id,
                rt.family_id,
            )
            raise HTTPException(
                status_code=401, detail="refresh token 重用，整批已撤銷"
            )

        # F1：family absolute session lifetime——從 family 最早 token（首次登入）起算
        # 超過上限即拒絕 rotation 並撤整 family。rotation 路徑原本無此檢查（僅 JWT
        # fallback 路徑有），而每個正常員工 session 都走 rotation → S2 absolute lifetime
        # 對所有現役 session 失效，失竊/棄置的 refresh cookie 可無限期 rotate 延續登入。
        from utils.auth import JWT_ABSOLUTE_LIFETIME_HOURS

        family_birth = (
            session.query(func.min(StaffRefreshToken.created_at))
            .filter(StaffRefreshToken.family_id == rt.family_id)
            .scalar()
        )
        if (
            family_birth is not None
            and (now_taipei_naive() - family_birth).total_seconds() / 3600
            > JWT_ABSOLUTE_LIFETIME_HOURS
        ):
            session.query(StaffRefreshToken).filter(
                StaffRefreshToken.family_id == rt.family_id,
                StaffRefreshToken.revoked_at.is_(None),
            ).update(
                {"revoked_at": now_taipei_naive()},
                synchronize_session=False,
            )
            # commit BEFORE raise（與 reuse 分支一致；get_session 路徑無自動 rollback 保護）
            session.commit()
            logger.warning(
                "[staff-refresh] absolute lifetime exceeded user_id=%s family_id=%s",
                rt.user_id,
                rt.family_id,
            )
            raise HTTPException(
                status_code=401,
                detail=(
                    f"登入工作階段已超過 {JWT_ABSOLUTE_LIFETIME_HOURS} 小時上限，"
                    "請重新登入"
                ),
            )

        # 正常 rotation：mark old token used + 生成新 token
        rt.used_at = now_taipei_naive()
        new_raw = secrets.token_urlsafe(48)
        new_hash = _hash_token(new_raw)
        new_rt = StaffRefreshToken(
            user_id=rt.user_id,
            family_id=rt.family_id,
            token_hash=new_hash,
            parent_token_id=rt.id,
            expires_at=now_taipei_naive() + REFRESH_LIFETIME,
            user_agent=(user_agent or "")[:255] or None,
            ip=(ip or "")[:45] or None,
        )
        session.add(new_rt)
        session.flush()
        user_id = rt.user_id
        session.commit()
    finally:
        session.close()

    return new_raw, user_id


def revoke_family(user_id: int, family_id: str) -> int:
    """Per-session revoke：撤銷同一 family 所有未撤 token。"""
    with session_scope() as session:
        n = (
            session.query(StaffRefreshToken)
            .filter(
                StaffRefreshToken.user_id == user_id,
                StaffRefreshToken.family_id == family_id,
                StaffRefreshToken.revoked_at.is_(None),
            )
            .update(
                {"revoked_at": now_taipei_naive()},
                synchronize_session=False,
            )
        )
    return n


def revoke_all_for_user(user_id: int) -> int:
    """Logout-all：revoke 所有 family + bump token_version。"""
    with session_scope() as session:
        n = (
            session.query(StaffRefreshToken)
            .filter(
                StaffRefreshToken.user_id == user_id,
                StaffRefreshToken.revoked_at.is_(None),
            )
            .update(
                {"revoked_at": now_taipei_naive()},
                synchronize_session=False,
            )
        )
        user = session.query(User).filter(User.id == user_id).first()
        if user is not None:
            user.token_version = (user.token_version or 0) + 1
    return n
