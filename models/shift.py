"""
models/shift.py — 排班相關模型
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, Date, DateTime, Boolean, ForeignKey, Text, UniqueConstraint, Index
from sqlalchemy.orm import relationship

from models.base import Base


class ShiftType(Base):
    """班別模板表"""
    __tablename__ = "shift_types"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False, comment="班別名稱")
    work_start = Column(String(5), nullable=False, comment="上班時間 HH:MM")
    work_end = Column(String(5), nullable=False, comment="下班時間 HH:MM")
    sort_order = Column(Integer, default=0, comment="排序")
    is_active = Column(Boolean, default=True, comment="是否啟用")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ShiftAssignment(Base):
    """每週排班表"""
    __tablename__ = "shift_assignments"
    __table_args__ = (
        UniqueConstraint("employee_id", "week_start_date", name="uq_shift_employee_week"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    shift_type_id = Column(Integer, ForeignKey("shift_types.id"), nullable=False)
    week_start_date = Column(Date, nullable=False, comment="該週週一日期")
    notes = Column(Text, comment="備註")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    employee = relationship("Employee", backref="shift_assignments")
    shift_type = relationship("ShiftType", backref="assignments")


class DailyShift(Base):
    """每日排班（調班/換班）表"""
    __tablename__ = "daily_shifts"
    __table_args__ = (
        UniqueConstraint("employee_id", "date", name="uq_daily_shift_employee_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    shift_type_id = Column(Integer, ForeignKey("shift_types.id"), nullable=True,
                           comment="班別（NULL 表示該日明確排休，不繼承週排班）")
    date = Column(Date, nullable=False, comment="排班日期")
    notes = Column(Text, comment="備註")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    employee = relationship("Employee", backref="daily_shifts")
    shift_type = relationship("ShiftType", backref="daily_shifts")


class ShiftSwapRequest(Base):
    """換班申請表"""
    __tablename__ = "shift_swap_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    requester_id = Column(Integer, ForeignKey("employees.id"), nullable=False, comment="發起人")
    target_id = Column(Integer, ForeignKey("employees.id"), nullable=False, comment="換班對象")
    swap_date = Column(Date, nullable=False, comment="換班日期")
    requester_shift_type_id = Column(Integer, ForeignKey("shift_types.id"), comment="發起者原班別")
    target_shift_type_id = Column(Integer, ForeignKey("shift_types.id"), comment="對象原班別")
    reason = Column(Text, comment="申請原因")
    status = Column(String(20), default="pending", comment="pending/accepted/rejected/cancelled")
    target_responded_at = Column(DateTime, comment="對方回覆時間")
    target_remark = Column(Text, comment="對方備註")
    executed_at = Column(DateTime, comment="執行時間")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_swap_requester", "requester_id", "status"),
        Index("ix_swap_target", "target_id", "status"),
    )

    requester = relationship("Employee", foreign_keys=[requester_id], backref="swap_requests_sent")
    target = relationship("Employee", foreign_keys=[target_id], backref="swap_requests_received")
    requester_shift_type = relationship("ShiftType", foreign_keys=[requester_shift_type_id])
    target_shift_type = relationship("ShiftType", foreign_keys=[target_shift_type_id])
