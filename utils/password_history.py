"""Password history replay prevention.

每次密碼變更時 record(user_id, hash)；下次變更前 assert_not_recently_used
比對最近 N 個 hash。N=5（NIST SP 800-63B 建議 5-10）。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from models.auth import PasswordHistory
from utils.auth import verify_password

logger = logging.getLogger(__name__)

PASSWORD_HISTORY_DEPTH = 5


def assert_not_recently_used(
    session: Session, user_id: int, new_plaintext_password: str
) -> None:
    """檢查 new_plaintext_password 是否與最近 PASSWORD_HISTORY_DEPTH 個 hash 相符。

    命中則 raise HTTPException(400)。比對方式：對每筆歷史 hash call
    verify_password（含 salt + iterations 解析）。
    """
    rows = (
        session.query(PasswordHistory)
        .filter(PasswordHistory.user_id == user_id)
        .order_by(desc(PasswordHistory.created_at), desc(PasswordHistory.id))
        .limit(PASSWORD_HISTORY_DEPTH)
        .all()
    )
    for row in rows:
        if verify_password(new_plaintext_password, row.password_hash):
            raise HTTPException(
                status_code=400,
                detail=f"不可重複使用最近 {PASSWORD_HISTORY_DEPTH} 個密碼",
            )


def record(session: Session, user_id: int, password_hash: str) -> None:
    """記錄一筆密碼變更歷史。caller 應在 user.password_hash = new_hash 後呼叫。"""
    session.add(PasswordHistory(user_id=user_id, password_hash=password_hash))
    session.flush()
