"""models/contact_book.py — 每日聯絡簿（家長入口 v3.1 Phase 1）

三張表：
- StudentContactBookEntry：每位學生每天一筆，結構化欄位 + 草稿/發布狀態 + 樂觀鎖
- StudentContactBookAck：家長已讀回條（仿 EventAcknowledgment 設計）
- StudentContactBookReply：家長簡短回覆，可軟刪除

照片附件沿用 Attachment polymorphic（owner_type='contact_book_entry'）。
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)

from models.base import Base

# 心情枚舉（前端用對應 emoji 顯示，後端不強檢但建議使用）
CONTACT_BOOK_MOODS = ("happy", "normal", "tired", "sad", "sick")
# 大便狀態
CONTACT_BOOK_BOWEL = ("none", "normal", "loose", "constipated")


class StudentContactBookEntry(Base):
    """每位學生每日聯絡簿一筆（同 student_id + log_date 唯一）。

    全部欄位 nullable — 老師當天看到什麼填什麼，不強制全填。
    `published_at IS NULL` 表示草稿；發布後家長端才看得到。
    `version` 為樂觀鎖欄位（PUT with `If-Match` header → 409 stale）。
    """

    __tablename__ = "student_contact_book_entries"
    __table_args__ = (
        # SQLite 不支援 partial unique index；改用條件查詢過濾 deleted_at IS NULL
        # 即可達到等價語意。此 UniqueConstraint 仍涵蓋 (student_id, log_date) 主鍵唯一。
        UniqueConstraint("student_id", "log_date", name="uq_contact_book_student_date"),
        Index("ix_contact_book_classroom_date", "classroom_id", "log_date"),
        Index("ix_contact_book_published", "published_at"),
        Index("ix_contact_book_deleted", "deleted_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=False)
    log_date = Column(Date, nullable=False)

    # 結構化欄位（皆 nullable）
    mood = Column(String(20), nullable=True)
    meal_lunch = Column(SmallInteger, nullable=True, comment="0-3 份量級距")
    meal_snack = Column(SmallInteger, nullable=True, comment="0-3 份量級距")
    nap_minutes = Column(SmallInteger, nullable=True)
    bowel = Column(String(20), nullable=True)
    temperature_c = Column(Numeric(4, 1), nullable=True)
    teacher_note = Column(Text, nullable=True)
    learning_highlight = Column(Text, nullable=True)

    created_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    published_at = Column(
        DateTime,
        nullable=True,
        comment="NULL=草稿；非 NULL 表示已發布家長可見",
    )
    version = Column(
        Integer, nullable=False, default=1, server_default="1", comment="樂觀鎖"
    )
    deleted_at = Column(DateTime, nullable=True, comment="軟刪除")

    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )


class StudentContactBookAck(Base):
    """家長已讀回條。每對 (entry, guardian_user) 唯一一筆。"""

    __tablename__ = "student_contact_book_acks"
    __table_args__ = (
        UniqueConstraint(
            "entry_id", "guardian_user_id", name="uq_contact_book_ack_entry_guardian"
        ),
        Index("ix_contact_book_ack_entry", "entry_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_id = Column(
        Integer,
        ForeignKey("student_contact_book_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    guardian_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    read_at = Column(DateTime, nullable=False, default=datetime.now)


class StudentContactBookReply(Base):
    """家長對某日聯絡簿的簡短回覆（≤500 字）。可軟刪除。"""

    __tablename__ = "student_contact_book_replies"
    __table_args__ = (
        Index("ix_contact_book_reply_entry_created", "entry_id", "created_at"),
        Index("ix_contact_book_reply_deleted", "deleted_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entry_id = Column(
        Integer,
        ForeignKey("student_contact_book_entries.id", ondelete="CASCADE"),
        nullable=False,
    )
    guardian_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body = Column(Text, nullable=False)
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
