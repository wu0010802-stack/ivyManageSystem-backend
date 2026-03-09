"""
models/overtime.py — 加班記錄與補打卡申請模型
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, Float, Date, DateTime, Boolean, ForeignKey, Index, Text
from sqlalchemy.orm import relationship

from models.base import Base


class OvertimeRecord(Base):
    """加班記錄表"""
    __tablename__ = "overtime_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    overtime_date = Column(Date, nullable=False, comment="加班日期")
    overtime_type = Column(String(20), nullable=False, comment="加班類型: weekday/weekend/holiday")

    start_time = Column(DateTime, comment="加班開始時間")
    end_time = Column(DateTime, comment="加班結束時間")
    hours = Column(Float, default=0, comment="加班時數")

    overtime_pay = Column(Float, default=0, comment="加班費（自動計算）")

    is_approved = Column(Boolean, nullable=True, default=None, comment="是否核准 (None=待審核, True=核准, False=駁回)")
    approved_by = Column(String(50), comment="核准人")
    reason = Column(Text, comment="加班原因")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    @property
    def approval_status(self) -> str:
        """語意化審核狀態，取代直接比較 nullable boolean 的反模式。
        回傳值：'pending' | 'approved' | 'rejected'"""
        if self.is_approved is True:
            return 'approved'
        if self.is_approved is False:
            return 'rejected'
        return 'pending'

    __table_args__ = (
        Index('ix_overtime_emp_date', 'employee_id', 'overtime_date'),
    )

    employee = relationship("Employee", backref="overtimes")


class PunchCorrectionRequest(Base):
    """補打卡申請表"""
    __tablename__ = "punch_correction_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    attendance_date = Column(Date, nullable=False, comment="欲補打的日期")
    correction_type = Column(String(20), nullable=False, comment="補正類型: punch_in / punch_out / both")
    requested_punch_in = Column(DateTime, nullable=True, comment="申請的上班時間")
    requested_punch_out = Column(DateTime, nullable=True, comment="申請的下班時間")
    reason = Column(Text, nullable=True, comment="說明原因")

    is_approved = Column(Boolean, nullable=True, default=None, comment="是否核准 (None=待審核, True=核准, False=駁回)")
    approved_by = Column(String(50), nullable=True, comment="核准人")
    rejection_reason = Column(Text, nullable=True, comment="駁回原因")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    @property
    def approval_status(self) -> str:
        """語意化審核狀態，取代直接比較 nullable boolean 的反模式。
        回傳值：'pending' | 'approved' | 'rejected'"""
        if self.is_approved is True:
            return 'approved'
        if self.is_approved is False:
            return 'rejected'
        return 'pending'

    __table_args__ = (
        Index('ix_punch_correction_emp_date', 'employee_id', 'attendance_date'),
    )

    employee = relationship("Employee", backref="punch_correction_requests")
