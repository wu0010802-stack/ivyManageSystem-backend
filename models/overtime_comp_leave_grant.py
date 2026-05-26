"""補休 grant ledger — per-OT 一筆紀錄。

語義：每筆核准的「以補休代加班費」OT 對應一筆 grant，
granted_at = ot.overtime_date, expires_at = granted_at + 1 年。
consumed_hours 由補休假單核准/駁回 FIFO 維護。
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import relationship

from models.base import Base


class OvertimeCompLeaveGrant(Base):
    __tablename__ = "overtime_comp_leave_grants"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    overtime_record_id = Column(
        Integer,
        ForeignKey("overtime_records.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    employee_id = Column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    granted_hours = Column(Float, nullable=False)
    granted_at = Column(Date, nullable=False)
    expires_at = Column(Date, nullable=False)
    consumed_hours = Column(Float, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="active")
    expired_at = Column(DateTime, nullable=True)
    reminder_sent_at = Column(
        DateTime, nullable=True, comment="LINE 推播提醒已發送時間（防重複）"
    )
    payout_salary_record_id = Column(
        Integer, ForeignKey("salary_records.id", ondelete="SET NULL"), nullable=True
    )
    payout_log_id = Column(
        BigInteger,
        ForeignKey("unused_leave_payout_log.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    overtime_record = relationship("OvertimeRecord", backref="comp_leave_grant")

    def __init__(self, **kwargs):
        """初始化 grant，設定預設值"""
        if "consumed_hours" not in kwargs:
            kwargs["consumed_hours"] = 0.0
        if "status" not in kwargs:
            kwargs["status"] = "active"
        super().__init__(**kwargs)

    __table_args__ = (
        CheckConstraint(
            "consumed_hours <= granted_hours", name="ck_grant_consumed_le_granted"
        ),
        Index("ix_grant_emp_status_expires", "employee_id", "status", "expires_at"),
        Index(
            "ix_grant_status_expires_active",
            "expires_at",
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )
