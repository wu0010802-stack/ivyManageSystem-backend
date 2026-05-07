"""
models/dismissal.py — 接送通知資料模型
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Text,
    ForeignKey,
    Index,
)

from models.base import Base

_TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def _now_taipei_naive() -> datetime:
    """以台灣時間取當下並去 tzinfo（DateTime 欄位 naive）。

    Why: 若主機部署在 UTC，default=datetime.now 會寫入 UTC 時刻；list_dismissal_calls
    用 date.today() 篩 TAIPEI 日，會造成台北時間 07:30-08:00 接送從「今日列表」消失。
    """
    return datetime.now(_TAIPEI_TZ).replace(tzinfo=None)


class StudentDismissalCall(Base):
    """家長接送通知單"""

    __tablename__ = "student_dismissal_calls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=False)
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    requested_at = Column(DateTime, default=_now_taipei_naive, nullable=False)
    # pending / acknowledged / completed / cancelled
    status = Column(String(20), default="pending", nullable=False)
    acknowledged_by_employee_id = Column(
        Integer, ForeignKey("employees.id"), nullable=True
    )
    acknowledged_at = Column(DateTime, nullable=True)
    completed_by_employee_id = Column(
        Integer, ForeignKey("employees.id"), nullable=True
    )
    completed_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_dismissal_calls_student_active", "student_id", "status"),
        Index("ix_dismissal_calls_classroom_status", "classroom_id", "status"),
        Index("ix_dismissal_calls_requested_at", "requested_at"),
    )
