"""models/guardian.py — 學生監護人（家長）資料表

一個學生可對應多位監護人，支援關係類型、是否主要聯絡人、緊急聯絡、
接送授權等旗標。取代原本 `students.parent_name/parent_phone` 單一欄位，
並將 `emergency_contact_*` 正規化為多筆。
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
)

from models.base import Base


GUARDIAN_RELATIONS = ["父親", "母親", "祖父", "祖母", "外公", "外婆", "監護人", "其他"]


class Guardian(Base):
    """學生監護人資料表"""

    __tablename__ = "guardians"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(
        Integer,
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(50), nullable=False)
    phone = Column(String(20), nullable=True)
    email = Column(String(100), nullable=True)
    relation = Column(String(20), nullable=True, comment="與學生關係")
    is_primary = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="是否為主要聯絡人（同一學生至多一位）",
    )
    is_emergency = Column(
        Boolean, default=False, nullable=False, comment="是否緊急聯絡人"
    )
    can_pickup = Column(Boolean, default=False, nullable=False, comment="是否授權接送")
    custody_note = Column(
        Text, nullable=True, comment="監護權說明（如離婚探視、單方監護等）"
    )
    sort_order = Column(Integer, default=0, nullable=False)
    deleted_at = Column(DateTime, nullable=True, comment="軟刪除時間")

    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )

    __table_args__ = (
        Index("ix_guardians_student", "student_id"),
        Index("ix_guardians_student_active", "student_id", "deleted_at"),
        Index("ix_guardians_phone", "phone"),
    )
