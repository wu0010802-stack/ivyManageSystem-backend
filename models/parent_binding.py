"""models/parent_binding.py — 家長帳號綁定相關資料表

家長 LIFF 登入時，若 LINE userId 尚未對應到 User，需要透過行政端簽發的
一次性綁定碼（GuardianBindingCode）建立 Guardian.user_id 關聯。

設計重點：
- 明碼僅回傳行政一次（並寫入 audit log），DB 只存 sha256 hash
- 24h 過期、一次性（used_at 落印後不可重用）
- claim 必走 atomic UPDATE WHERE used_at IS NULL（防 race），不走 SELECT 再 UPDATE
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Index,
)

from models.base import Base


class GuardianBindingCode(Base):
    """家長綁定一次性碼

    行政人員對特定 Guardian 簽發；家長 LIFF 登入後輸入明碼完成 claim。
    """

    __tablename__ = "guardian_binding_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guardian_id = Column(
        Integer,
        ForeignKey("guardians.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code_hash = Column(
        String(64),
        nullable=False,
        unique=True,
        comment="sha256(明碼) hex；明碼僅回傳簽發者一次",
    )
    expires_at = Column(DateTime, nullable=False, comment="預設 24h 過期")
    used_at = Column(
        DateTime,
        nullable=True,
        comment="claim 成功時間；non-null 即視為已用，不可重用",
    )
    used_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="claim 該碼的家長 User",
    )
    created_by = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=False,
        comment="簽發此碼的行政 User（稽核用）",
    )
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    __table_args__ = (
        Index(
            "ix_guardian_binding_expires_unused",
            "expires_at",
            "used_at",
        ),
    )
