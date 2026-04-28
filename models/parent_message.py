"""models/parent_message.py — 家園溝通平台（家長入口 2.0 Phase 3）

包含：
- ParentMessageThread     1對1（parent, teacher, student）三元組 thread
- ParentMessage           append-only 訊息（30 分內 sender 可以 deleted_at 撤回）
- LineWebhookEvent        LINE webhook 事件去重表（schema 先建，Phase 5 啟用）

設計重點：
- thread 存 student_id：UI 端依「孩子」群組、LINE webhook 路由 thread 都需要
- 不每訊息一筆 read receipt，用 thread.parent_last_read_at / teacher_last_read_at 兩欄
- client_request_id 為前端產生的 UUID，partial UNIQUE 提供冪等
- source = 'app' | 'line' 標記訊息來源（Phase 5 LINE 雙向會用 'line'）
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
    Text,
    UniqueConstraint,
)

from models.base import Base


class ParentMessageThread(Base):
    """家長 ↔ 教師 1對1 thread。

    UNIQUE(parent_user_id, teacher_user_id, student_id)：
        同一三元組僅一個 thread；首次寫訊息時 upsert thread。
    """

    __tablename__ = "parent_message_threads"
    __table_args__ = (
        UniqueConstraint(
            "parent_user_id",
            "teacher_user_id",
            "student_id",
            name="uq_parent_thread_triple",
        ),
        Index(
            "ix_parent_thread_parent_lastmsg",
            "parent_user_id",
            "last_message_at",
        ),
        Index(
            "ix_parent_thread_teacher_lastmsg",
            "teacher_user_id",
            "last_message_at",
        ),
        Index("ix_parent_thread_student", "student_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    parent_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    teacher_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    student_id = Column(
        Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )

    last_message_at = Column(DateTime, nullable=True, index=False)
    parent_last_read_at = Column(DateTime, nullable=True)
    teacher_last_read_at = Column(DateTime, nullable=True)

    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )


class ParentMessage(Base):
    """append-only 訊息。

    sender_role = 'parent' | 'teacher'。撤回採 deleted_at tombstone（30 分鐘內 sender 可撤）。
    body 可為空（純附件訊息允許）。client_request_id = 前端產生 UUID，partial UNIQUE 防重送。
    source 標 'app' / 'line'（Phase 5 LINE webhook 寫入用）。
    """

    __tablename__ = "parent_messages"
    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "client_request_id",
            name="uq_parent_msg_client_request",
        ),
        Index("ix_parent_msg_thread_created", "thread_id", "created_at"),
        Index("ix_parent_msg_sender", "sender_user_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_id = Column(
        Integer,
        ForeignKey("parent_message_threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    sender_role = Column(
        String(10),
        nullable=False,
        comment="'parent' 或 'teacher'",
    )
    body = Column(Text, nullable=True)
    client_request_id = Column(String(64), nullable=True)
    source = Column(
        String(10),
        nullable=False,
        default="app",
        server_default="app",
        comment="'app' = LIFF/portal，'line' = LINE webhook（Phase 5）",
    )
    deleted_at = Column(
        DateTime, nullable=True, comment="sender 30 分內撤回；UI 顯示「此訊息已撤回」"
    )
    created_at = Column(DateTime, default=datetime.now, nullable=False)


class LineReplyContext(Base):
    """LINE webhook 多孩家長 reply 路由上下文（Phase 5）。

    家長從 quick-reply 點選 thread → 寫一筆 context（line_user_id 唯一）；
    後續 10 分鐘內收到的純文字訊息歸到該 thread。Reply 後刷新 expires_at 滑動。
    """

    __tablename__ = "line_reply_contexts"
    __table_args__ = (
        UniqueConstraint("line_user_id", name="uq_line_reply_context_user"),
        Index("ix_line_reply_context_expires", "expires_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    line_user_id = Column(String(100), nullable=False)
    thread_id = Column(
        Integer,
        ForeignKey("parent_message_threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )


class LineWebhookEvent(Base):
    """LINE webhook 事件去重表（Phase 5 啟用；Phase 3 schema 先建好）。

    LINE 會 retry：同 webhookEventId 收到第二次時，UNIQUE 攔下後跳過處理。
    保留 30 天後由 cron GC（透過 created_at 索引）。
    """

    __tablename__ = "line_webhook_events"
    __table_args__ = (
        UniqueConstraint("webhook_event_id", name="uq_line_webhook_event_id"),
        Index("ix_line_webhook_user_created", "line_user_id", "created_at"),
        Index("ix_line_webhook_created", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    webhook_event_id = Column(String(64), nullable=False)
    event_type = Column(
        String(20),
        nullable=False,
        comment="'message' | 'postback' | 'follow' | ...",
    )
    line_user_id = Column(String(100), nullable=True)
    processed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
