"""
models/student_transfer.py — 學生轉班記錄表

每次呼叫 bulk_transfer_students 時寫入一筆，
用途：讓班級統計報表可依「指定日期的班級歸屬」查詢，
      而非只依學生當前 classroom_id 查詢。
"""

from datetime import datetime

from sqlalchemy import Column, Integer, DateTime, ForeignKey, Index

from models.base import Base


class StudentClassroomTransfer(Base):
    """學生轉班歷史記錄"""
    __tablename__ = "student_classroom_transfers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    # NULL 表示初次分班（無前一個班級）
    from_classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=True)
    to_classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=False)
    transferred_at = Column(DateTime, nullable=False, default=datetime.now)
    # 操作者（User.id）
    transferred_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        Index("ix_student_transfers_student", "student_id"),
        Index("ix_student_transfers_at", "transferred_at"),
        Index("ix_student_transfers_to_classroom", "to_classroom_id", "transferred_at"),
    )
