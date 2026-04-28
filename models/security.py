"""
models/security.py — 安全相關支援表

兩張支援表（皆 PG-only；測試環境用 SQLite + 應用層 fallback）：

- RateLimitBucket: 取代 in-process dict 限流；支援多 worker 部署。
- JwtBlocklist: jti 黑名單，支援使用者主動 logout 立即廢止 token。
"""

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    text,
)

from models.base import Base


class RateLimitBucket(Base):
    """滑動視窗限流計數（LOW-1）

    bucket_key 為 limiter 自定字串（例如 "login_ip:1.2.3.4"），
    window_start 為當前視窗的起點時間戳，count 為該視窗內 hit 次數。
    """

    __tablename__ = "rate_limit_buckets"

    bucket_key = Column(Text, nullable=False)
    window_start = Column(DateTime(timezone=True), nullable=False)
    count = Column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        PrimaryKeyConstraint("bucket_key", "window_start"),
        Index("ix_rate_limit_buckets_window_start", "window_start"),
    )


class JwtBlocklist(Base):
    """JWT jti 黑名單（LOW-2）

    logout 時把當前 token 的 jti 寫入；驗 token 時 SELECT 1 命中即拒絕。
    expires_at 等於 token 的原始過期時間 + 寬限期，過期可清除。
    """

    __tablename__ = "jwt_blocklist"

    jti = Column(Text, primary_key=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    reason = Column(Text, nullable=True)

    __table_args__ = (Index("ix_jwt_blocklist_expires_at", "expires_at"),)
