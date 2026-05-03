"""models/parent_refresh_token.py — 家長端長效 refresh token

設計重點：
- token raw 永不入庫；只存 sha256(raw) hex（64 字）
- family_id 串起同一裝置的 rotation 鏈；reuse 偵測時整 family revoke
- used_at != NULL 後再被送來 → reuse；單一 race 窗（5 秒）容忍同 token 雙請求
- expires_at 預設 now + 30 天；GC 7 天後刪
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from models.base import Base


class ParentRefreshToken(Base):
    __tablename__ = "parent_refresh_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SQLite 不支援 PG UUID；用 String(36) 跨方言相容
    family_id = Column(String(36), nullable=False, default=lambda: str(uuid.uuid4()))
    token_hash = Column(
        String(64),
        nullable=False,
        unique=True,
        comment="sha256(raw refresh token) hex；DB 不存明文",
    )
    parent_token_id = Column(
        BigInteger,
        ForeignKey("parent_refresh_tokens.id", ondelete="SET NULL"),
        nullable=True,
        comment="rotation 上一個 token；可追溯 family",
    )
    used_at = Column(DateTime, nullable=True, comment="rotation 後填入；reuse 偵測欄位")
    revoked_at = Column(DateTime, nullable=True, comment="family 全撤銷時填入")
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    user_agent = Column(String(255), nullable=True, comment="觀測用，不參與決策")
    ip = Column(String(45), nullable=True, comment="IPv6 預留；觀測用")

    __table_args__ = (
        Index("ix_parent_refresh_user_family", "user_id", "family_id"),
        Index("ix_parent_refresh_expires_at", "expires_at"),
    )
