"""
models/employee.py — 員工相關模型
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
    CHAR,
    Index,
)
from sqlalchemy.orm import relationship

from models.base import Base


class EmployeeType(enum.Enum):
    """員工類型"""

    REGULAR = "regular"  # 正職員工
    HOURLY = "hourly"  # 才藝老師 (時薪制)


class JobTitle(Base):
    __tablename__ = "job_titles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)


class Employee(Base):
    """員工表"""

    __tablename__ = "employees"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(String(20), unique=True, nullable=False, comment="工號")
    name = Column(String(50), nullable=False, comment="姓名")
    id_number = Column(String(20), comment="身分證字號")

    employee_type = Column(
        String(20),
        default=EmployeeType.REGULAR.value,
        comment="員工類型：regular/hourly",
    )
    title = Column(String(50), nullable=True, comment="職稱 (Legacy)")
    job_title_id = Column(
        Integer, ForeignKey("job_titles.id"), nullable=True, comment="職稱 ID"
    )
    position = Column(String(50), nullable=True, comment="職務 (Duty)")
    classroom_id = Column(
        Integer, ForeignKey("classrooms.id"), nullable=True, comment="所屬班級"
    )

    job_title_rel = relationship("JobTitle", backref="employees")

    @property
    def title_name(self) -> str:
        """統一的職稱名稱：優先使用 job_title_rel，fallback 到 legacy title 欄位"""
        return (self.job_title_rel.name if self.job_title_rel else self.title) or ""

    base_salary = Column(Float, default=0, comment="底薪")
    hourly_rate = Column(Float, default=0, comment="時薪（才藝老師用）")

    supervisor_allowance = Column(Float, default=0, comment="主管加給")
    teacher_allowance = Column(Float, default=0, comment="導師津貼")
    meal_allowance = Column(Float, default=0, comment="伙食津貼")
    transportation_allowance = Column(Float, default=0, comment="交通津貼")
    other_allowance = Column(Float, default=0, comment="其他津貼")

    bank_code = Column(String(10), comment="銀行代碼")
    bank_account = Column(String(30), comment="銀行帳號")
    bank_account_name = Column(String(50), comment="帳戶戶名")

    insurance_salary_level = Column(Float, default=0, comment="投保薪資級距")
    pension_self_rate = Column(Float, default=0, comment="勞退自提比例 (0~0.06)")

    work_start_time = Column(String(5), default="08:00", comment="上班時間 HH:MM")
    work_end_time = Column(String(5), default="17:00", comment="下班時間 HH:MM")

    is_active = Column(Boolean, default=True, comment="是否在職")
    resign_date = Column(Date, nullable=True, comment="離職日期")
    resign_reason = Column(String(200), nullable=True, comment="離職原因")
    bonus_grade = Column(
        CHAR(1), nullable=True, comment="節慶獎金等級覆蓋 (A/B/C)，NULL=依職稱自動判斷"
    )
    supervisor_role = Column(
        String(20), nullable=True, comment="主管職 (園長/主任/組長/副組長)"
    )
    is_office_staff = Column(
        Boolean, default=False, comment="是否為辦公室人員（舊欄位，停用）"
    )
    dependents = Column(Integer, default=0, comment="眷屬人數（健保計算用）")
    hire_date = Column(Date, comment="到職日期")
    probation_end_date = Column(Date, nullable=True, comment="試用期結束日")
    birthday = Column(Date, nullable=True, comment="生日")

    phone = Column(String(20), comment="聯絡電話")
    address = Column(String(200), comment="通訊地址")
    emergency_contact_name = Column(String(50), comment="緊急聯絡人")
    emergency_contact_phone = Column(String(20), comment="緊急聯絡人電話")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_employee_active_resign", "is_active", "resign_date"),
        Index("ix_employee_job_title_id", "job_title_id"),
        Index("ix_employee_classroom_id", "classroom_id"),
        Index("ix_employee_is_active", "is_active"),
    )

    attendances = relationship(
        "Attendance", back_populates="employee", cascade="all, delete-orphan"
    )
    leaves = relationship(
        "LeaveRecord",
        foreign_keys="[LeaveRecord.employee_id]",
        back_populates="employee",
        cascade="all, delete-orphan",
    )
    salaries = relationship(
        "SalaryRecord", back_populates="employee", cascade="all, delete-orphan"
    )
