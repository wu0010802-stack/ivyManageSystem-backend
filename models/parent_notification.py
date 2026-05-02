"""models/parent_notification.py — 家長通知偏好（家長入口 2.0 Phase 6）

設計：稀疏 row 模型 — row 缺 = enabled（預設全開）；row 存在 = 看 enabled 欄。
新增 event_type 不需資料遷移。

UNIQUE(user_id, event_type, channel)：每組偏好唯一一筆。
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)

from models.base import Base

# 預先列舉前端可選的 event_type；後端不強檢以保留擴充彈性
PARENT_NOTIFICATION_EVENT_TYPES = (
    "message_received",  # 老師訊息
    "announcement",  # 園所公告
    "event_ack_required",  # 事件待簽
    "fee_due",  # 學費到期
    "leave_result",  # 學生請假審核結果
    "attendance_alert",  # 出席異常
    "contact_book_published",  # 每日聯絡簿發布（v3.1）
)

PARENT_NOTIFICATION_CHANNELS = ("line",)  # v1 只支援 LINE


class ParentNotificationPreference(Base):
    """家長通知偏好（稀疏 row）。"""

    __tablename__ = "parent_notification_preferences"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "event_type",
            "channel",
            name="uq_parent_notif_pref_triple",
        ),
        Index("ix_parent_notif_pref_user", "user_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_type = Column(String(40), nullable=False)
    channel = Column(
        String(10),
        nullable=False,
        default="line",
        server_default="line",
    )
    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="false 即關閉該 event 的該 channel 推播",
    )

    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )
