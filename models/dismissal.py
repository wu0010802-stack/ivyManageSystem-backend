"""
models/dismissal.py — 接送通知資料模型
"""

from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, DateTime, Text,
    ForeignKey, Index,
)

from models.base import Base


class StudentDismissalCall(Base):
    """家長接送通知單"""
    __tablename__ = "student_dismissal_calls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=False)
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    requested_at = Column(DateTime, default=datetime.now, nullable=False)
    # pending / acknowledged / completed / cancelled
    status = Column(String(20), default="pending", nullable=False)
    acknowledged_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    completed_by_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_dismissal_calls_student_active", "student_id", "status"),
        Index("ix_dismissal_calls_classroom_status", "classroom_id", "status"),
        Index("ix_dismissal_calls_requested_at", "requested_at"),
    )
