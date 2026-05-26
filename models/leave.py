"""
models/leave.py — 請假記錄與配額模型
"""

import enum
from datetime import datetime

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
    text,
)
from sqlalchemy.orm import relationship

from models.base import Base


class LeaveType(enum.Enum):
    """請假類型"""

    SICK = "sick"
    PERSONAL = "personal"
    MENSTRUAL = "menstrual"
    ANNUAL = "annual"
    MATERNITY = "maternity"
    PATERNITY = "paternity"


class LeaveRecord(Base):
    """請假記錄表"""

    __tablename__ = "leave_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    leave_type = Column(String(20), nullable=False, comment="請假類型")
    start_date = Column(Date, nullable=False, comment="開始日期")
    end_date = Column(Date, nullable=False, comment="結束日期")
    start_time = Column(String(5), nullable=True, comment="開始時間 HH:MM")
    end_time = Column(String(5), nullable=True, comment="結束時間 HH:MM")
    leave_hours = Column(Float, default=8, comment="請假時數")

    is_deductible = Column(Boolean, default=True, comment="是否扣薪")
    deduction_ratio = Column(Float, default=1.0, comment="扣薪比例")

    # 病假住院旗標（勞工請假規則第 4 條：未住院 30 天/年；住院 1 年/年）
    is_hospitalized = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="病假是否為住院（影響年度配額計算）",
    )

    reason = Column(Text, comment="請假原因")
    attachment_paths = Column(Text, nullable=True, comment="附件路徑清單（JSON 陣列）")

    is_approved = Column(
        Boolean,
        nullable=True,
        default=None,
        comment="是否核准 (None=待審核, True=核准, False=駁回)",
    )
    status = Column(
        String(20),
        nullable=False,
        server_default="pending",
        comment="審核狀態：pending / approved / rejected（P1 dual-write SoT）",
    )
    approved_by = Column(String(50), comment="核准人")
    rejection_reason = Column(Text, nullable=True, comment="駁回原因")

    # 補休假單來源加班記錄（僅 leave_type='compensatory' 時有意義）
    source_overtime_id = Column(
        Integer,
        ForeignKey("overtime_records.id", ondelete="SET NULL"),
        nullable=True,
        comment="來源加班記錄 ID（補休專用）",
    )

    # ── 職務代理人欄位 ──────────────────────────────────────────────────────
    substitute_employee_id = Column(
        Integer,
        ForeignKey("employees.id"),
        nullable=True,
        index=True,
        comment="代理人員工 ID",
    )
    substitute_status = Column(
        String(20),
        default="not_required",
        comment="代理狀態：not_required/pending/accepted/rejected",
    )
    substitute_responded_at = Column(DateTime, nullable=True, comment="代理人回覆時間")
    substitute_remark = Column(Text, nullable=True, comment="代理人備註")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    @property
    def approval_status(self) -> str:
        """語意化審核狀態。P1 起內部走新 status column；既有 caller 不必改動。
        回傳值：'pending' | 'approved' | 'rejected'"""
        return self.status

    __table_args__ = (
        Index("ix_leave_emp_dates", "employee_id", "start_date", "end_date"),
        Index("ix_leave_emp_approved", "employee_id", "is_approved"),
        Index("ix_leave_approved_start_date", "is_approved", "start_date"),
        Index("ix_leave_emp_type_approved", "employee_id", "leave_type", "is_approved"),
        # P1: new status-prefixed indexes (mirror the is_approved ones).
        # is_approved indexes are dropped in P4.
        Index("ix_leave_emp_status", "employee_id", "status"),
        Index("ix_leave_status_start_date", "status", "start_date"),
        Index("ix_leave_emp_type_status", "employee_id", "leave_type", "status"),
        Index("ix_leave_status_date", "status", "start_date"),
    )

    employee = relationship(
        "Employee", foreign_keys=[employee_id], back_populates="leaves"
    )
    substitute = relationship(
        "Employee", foreign_keys=[substitute_employee_id], backref="substitute_leaves"
    )
    source_overtime = relationship(
        "OvertimeRecord",
        foreign_keys=[source_overtime_id],
        backref="comp_leave_records",
    )


class LeaveQuota(Base):
    """請假配額表（年度）— 僅儲存配額總量，已使用量動態從 LeaveRecord 計算"""

    __tablename__ = "leave_quotas"
    __table_args__ = (
        Index(
            "uq_leave_quota_legacy",
            "employee_id",
            "year",
            "leave_type",
            unique=True,
            postgresql_where=text("school_year IS NULL"),
            sqlite_where=text("school_year IS NULL"),
        ),
        Index("ix_leave_quota_year", "year"),
        Index(
            "uq_leave_quotas_employee_school_year_type",
            "employee_id",
            "school_year",
            "leave_type",
            unique=True,
            postgresql_where=text("school_year IS NOT NULL"),
            sqlite_where=text("school_year IS NOT NULL"),
        ),
        Index(
            "ix_leave_quotas_school_year",
            "school_year",
            postgresql_where=text("school_year IS NOT NULL"),
            sqlite_where=text("school_year IS NOT NULL"),
        ),
        Index(
            "uq_leave_quotas_emp_period_annual",
            "employee_id",
            "period_start",
            "leave_type",
            unique=True,
            postgresql_where=text("period_start IS NOT NULL AND leave_type = 'annual'"),
            sqlite_where=text("period_start IS NOT NULL AND leave_type = 'annual'"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    year = Column(Integer, nullable=False, comment="適用年度")
    school_year = Column(
        Integer,
        nullable=True,
        comment="民國學年；null = legacy year-based row",
    )
    leave_type = Column(String(20), nullable=False, comment="假別")
    total_hours = Column(Float, nullable=False, comment="年度配額時數")
    note = Column(String(200), nullable=True, comment="備註（如年資計算依據）")
    period_start = Column(
        Date, nullable=True, comment="週年制配額起日（hire_date 基準）"
    )
    period_end = Column(
        Date, nullable=True, comment="週年制配額迄日（+1y，2/29 fallback 2/28）"
    )

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    employee = relationship("Employee", backref="leave_quotas")
