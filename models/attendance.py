"""
models/attendance.py — 考勤記錄模型
"""

import enum
from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Text,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from models.base import Base


class AttendanceStatus(enum.Enum):
    """考勤狀態"""

    NORMAL = "normal"
    LATE = "late"
    EARLY_LEAVE = "early_leave"
    MISSING_PUNCH = "missing"
    ABSENT = "absent"
    LEAVE = "leave"  # 全天請假（員工請假同步寫入考勤）


class Attendance(Base):
    """考勤記錄表"""

    __tablename__ = "attendances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    attendance_date = Column(Date, nullable=False, comment="考勤日期")
    punch_in_time = Column(DateTime, comment="上班打卡時間")
    punch_out_time = Column(DateTime, comment="下班打卡時間")

    status = Column(
        String(20), default=AttendanceStatus.NORMAL.value, comment="考勤狀態"
    )
    is_late = Column(Boolean, default=False, comment="是否遲到")
    is_early_leave = Column(Boolean, default=False, comment="是否早退")
    is_missing_punch_in = Column(Boolean, default=False, comment="是否未打卡（上班）")
    is_missing_punch_out = Column(Boolean, default=False, comment="是否未打卡（下班）")

    late_minutes = Column(Integer, default=0, comment="遲到分鐘數")
    early_leave_minutes = Column(Integer, default=0, comment="早退分鐘數")

    remark = Column(Text, comment="備註")

    # 異常確認欄位
    confirmed_action = Column(
        String(20),
        nullable=True,
        comment="確認動作：accept/use_pto/dispute/admin_accept/admin_waive",
    )
    confirmed_by = Column(String(100), nullable=True, comment="確認操作者")
    confirmed_at = Column(DateTime, nullable=True, comment="確認時間")

    leave_record_id = Column(
        Integer,
        ForeignKey("leave_records.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="關聯請假單 ID（全天/半天假同步寫入時填入）",
    )
    partial_leave_hours = Column(
        Numeric(4, 2),
        nullable=True,
        comment="半天/部分假時數（null = 全天或無假）",
    )

    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)

    __table_args__ = (
        Index("ix_attendance_emp_date", "employee_id", "attendance_date"),
        Index("ix_attendance_date", "attendance_date"),
        Index(
            "ix_attendance_anomaly",
            "attendance_date",
            "is_late",
            "is_early_leave",
            "is_missing_punch_in",
            "is_missing_punch_out",
        ),
        UniqueConstraint(
            "employee_id", "attendance_date", name="uq_attendance_employee_date"
        ),
    )

    employee = relationship("Employee", back_populates="attendances")
