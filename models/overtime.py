"""
models/overtime.py — 加班記錄與補打卡申請模型
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Date,
    DateTime,
    Boolean,
    ForeignKey,
    Index,
    Text,
)
from sqlalchemy.orm import relationship

from models.base import Base
from models.types import Money


class OvertimeRecord(Base):
    """加班記錄表"""

    __tablename__ = "overtime_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    overtime_date = Column(Date, nullable=False, comment="加班日期")
    overtime_type = Column(
        String(20), nullable=False, comment="加班類型: weekday/weekend/holiday"
    )

    start_time = Column(DateTime, comment="加班開始時間")
    end_time = Column(DateTime, comment="加班結束時間")
    hours = Column(Float, default=0, comment="加班時數")

    overtime_pay = Column(Money, default=0, comment="加班費（自動計算）")

    use_comp_leave = Column(
        Boolean, default=False, nullable=False, comment="是否以補休代替加班費"
    )
    comp_leave_granted = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="補休配額是否已發放（防止重複發放）",
    )

    status = Column(
        String(20),
        nullable=False,
        server_default="pending",
        comment="審核狀態：pending / approved / rejected（P1 dual-write SoT）",
    )
    approved_by = Column(String(50), comment="核准人")
    reason = Column(Text, comment="加班原因")

    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)

    @property
    def approval_status(self) -> str:
        """語意化審核狀態。P1 起內部走新 status column；既有 caller 不必改動。
        回傳值：'pending' | 'approved' | 'rejected'"""
        return self.status

    __table_args__ = (
        Index("ix_overtime_emp_date", "employee_id", "overtime_date"),
        Index("ix_overtime_emp_status", "employee_id", "status"),
        Index("ix_overtime_status_date", "status", "overtime_date"),
    )

    employee = relationship("Employee", backref="overtimes")


class PunchCorrectionRequest(Base):
    """補打卡申請表"""

    __tablename__ = "punch_correction_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    attendance_date = Column(Date, nullable=False, comment="欲補打的日期")
    correction_type = Column(
        String(20), nullable=False, comment="補正類型: punch_in / punch_out / both"
    )
    requested_punch_in = Column(DateTime, nullable=True, comment="申請的上班時間")
    requested_punch_out = Column(DateTime, nullable=True, comment="申請的下班時間")
    reason = Column(Text, nullable=True, comment="說明原因")

    status = Column(
        String(20),
        nullable=False,
        server_default="pending",
        comment="審核狀態：pending / approved / rejected（P1 dual-write SoT）",
    )
    approved_by = Column(String(50), nullable=True, comment="核准人")
    rejection_reason = Column(Text, nullable=True, comment="駁回原因")

    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)

    @property
    def approval_status(self) -> str:
        """語意化審核狀態。P1 起內部走新 status column；既有 caller 不必改動。
        回傳值：'pending' | 'approved' | 'rejected'"""
        return self.status

    __table_args__ = (
        Index("ix_punch_correction_emp_date", "employee_id", "attendance_date"),
        Index("ix_punch_correction_status", "status", "attendance_date"),
    )

    employee = relationship("Employee", backref="punch_correction_requests")
