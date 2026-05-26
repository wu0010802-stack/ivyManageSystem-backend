"""Magic-link token 服務：產生 / hash / 驗證 / 撤銷 / active 判斷。

設計：
- token 用 secrets.token_urlsafe(32) → 256-bit base64url random
- DB 存 sha256 hash，明文不留（同 password salt+hash 原則）
- TTL 30 天 + 3 次下載上限
- verify 失敗統一回 None（不暴露差異，防 enumeration）

設計參考：spec §8。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models.offboarding import EmployeeOffboardingRecord

logger = logging.getLogger(__name__)

TOKEN_TTL_DAYS = 30
MAX_DOWNLOADS = 3


class MagicLinkError(Exception):
    """magic-link 操作錯誤。"""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


def hash_token(token: str) -> str:
    """SHA-256 hash 明文 token。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token(session: Session, record: EmployeeOffboardingRecord) -> str:
    """產生新 token、寫 hash + expires + 歸 0 count + 清 revoked。

    回傳明文 token（只此一次出現；DB 只存 hash）。
    呼叫端負責 session.commit()。
    """
    token = secrets.token_urlsafe(32)
    record.magic_link_token_hash = hash_token(token)
    record.magic_link_expires_at = datetime.now() + timedelta(days=TOKEN_TTL_DAYS)  # noqa: DTZ005
    record.magic_link_revoked_at = None
    record.magic_link_download_count = 0
    record.magic_link_last_used_at = None
    session.flush()

    logger.warning(
        "magic-link token 產生：employee_id=%s expires_at=%s",
        record.employee_id,
        record.magic_link_expires_at,
    )
    return token


def verify_token(session: Session, token: str) -> Optional[EmployeeOffboardingRecord]:
    """驗證 token：合法且未過期未撤未達次數 → 回 record；否則回 None。

    不暴露差異原因（防 enumeration）— 呼叫端統一回 410 Gone。
    """
    if not token:
        return None
    token_hash = hash_token(token)
    record = (
        session.query(EmployeeOffboardingRecord)
        .filter_by(magic_link_token_hash=token_hash)
        .first()
    )
    if record is None:
        return None
    if record.magic_link_revoked_at is not None:
        return None
    if (
        record.magic_link_expires_at is not None
        and record.magic_link_expires_at < datetime.now()  # noqa: DTZ005
    ):
        return None
    if (record.magic_link_download_count or 0) >= MAX_DOWNLOADS:
        return None
    return record


def revoke_token(session: Session, record: EmployeeOffboardingRecord) -> None:
    """撤銷 token（保留 hash 行 audit）。呼叫端負責 commit。"""
    record.magic_link_revoked_at = datetime.now()  # noqa: DTZ005
    session.flush()
    logger.warning(
        "magic-link token 已撤：employee_id=%s",
        record.employee_id,
    )


def is_active(record: EmployeeOffboardingRecord) -> bool:
    """派生 bool：token 存在且未過期未撤未達次數。
    （與 api/offboarding._is_magic_link_active 同邏輯，集中於此供 reuse。）
    """
    if not record.magic_link_token_hash:
        return False
    if record.magic_link_revoked_at is not None:
        return False
    if (
        record.magic_link_expires_at is not None
        and record.magic_link_expires_at < datetime.now()  # noqa: DTZ005
    ):
        return False
    if (record.magic_link_download_count or 0) >= MAX_DOWNLOADS:
        return False
    return True


def record_download(session: Session, record: EmployeeOffboardingRecord) -> None:
    """記錄下載：count++ + last_used_at = now。呼叫端負責 commit。"""
    record.magic_link_download_count = (record.magic_link_download_count or 0) + 1
    record.magic_link_last_used_at = datetime.now()  # noqa: DTZ005
