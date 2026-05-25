"""通知中央 dispatcher 的持久層 row（in-app log + audit）。

單筆 row 代表一個 event 的完整 fan-out 結果（不是每通道一筆）。
三個 channels_* JSON 欄位記錄通道狀態：
- channels_attempted: 解析 matrix + preference gate 後實際嘗試的 channel
- channels_succeeded: 成功送出（含 in_app 寫 log 本身）
- channels_failed: [{"channel": "line", "error": "..."}, ...]

title/body/deep_link 由 renderer 預渲染寫入；payload_json 保留結構化 context
供前端深用（avatar / status chip）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    text,
)

from models.base import Base


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    __table_args__ = (
        Index(
            "ix_notif_log_recipient_unread",
            "recipient_user_id",
            "read_at",
            postgresql_where=text("read_at IS NULL"),
        ),
        Index("ix_notif_log_recipient_created", "recipient_user_id", "created_at"),
        Index("ix_notif_log_source", "source_entity_type", "source_entity_id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    recipient_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_type = Column(String(60), nullable=False)
    sender_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    title = Column(String(120), nullable=False)
    body = Column(Text, nullable=False)
    payload_json = Column(JSON, nullable=False, default=dict)
    source_entity_type = Column(String(40), nullable=True)
    source_entity_id = Column(Integer, nullable=True)
    deep_link = Column(String(255), nullable=True)
    channels_attempted = Column(JSON, nullable=False, default=list)
    channels_succeeded = Column(JSON, nullable=False, default=list)
    channels_failed = Column(JSON, nullable=False, default=list)
    read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
