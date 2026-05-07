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
from models.types import Money


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
    # 階段 2-D（2026-05-07）：節慶獎金等級對應從 hardcode 搬到 DB
    bonus_grade = Column(
        CHAR(1),
        nullable=True,
        comment="節慶獎金等級（A/B/C）；NULL=非帶班職稱不適用",
    )


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

    base_salary = Column(Money, default=0, comment="底薪")
    hourly_rate = Column(Money, default=0, comment="時薪（才藝老師用）")

    bank_code = Column(String(10), comment="銀行代碼")
    bank_account = Column(String(30), comment="銀行帳號")
    bank_account_name = Column(String(50), comment="帳戶戶名")

    insurance_salary_level = Column(Money, default=0, comment="投保薪資級距")
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

    # 階段 2-C（2026-05-07）：表達常見會計實務狀況的特殊欄位
    no_employment_insurance = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="免就保（退休再聘等）；勞保扣款改用 11.5% 不含就保 1%",
    )
    health_exempt = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="健保豁免（公保/老人健保等）；公司不扣本人+眷屬健保",
    )
    skip_payroll_bonuses = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="業主指示不發紅利/節慶/超額獎金（基本薪+保險仍正常計算）",
    )
    extra_dependents_quarterly = Column(
        Integer,
        default=0,
        nullable=False,
        comment="季扣眷屬人數；1/4/7/10 月份額外扣 health_employee × N × 3",
    )
    insurance_salary_override_reason = Column(
        String(200),
        nullable=True,
        comment="投保金額 ≠ 底薪 的合規記錄；純文字，不影響計算",
    )
    bypass_standard_base = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="True=計薪用 emp.base_salary（個人合約含年資加給）；False=走 PositionSalaryConfig 標準",
    )

    # 議題 B：勞保/健保/勞退 三制度分項投保金額（NULL=沿用 insurance_salary_level）
    labor_insured_salary = Column(
        Money,
        nullable=True,
        comment="勞保獨立投保金額；NULL=沿用 insurance_salary_level",
    )
    health_insured_salary = Column(
        Money,
        nullable=True,
        comment="健保獨立投保金額；NULL=沿用 insurance_salary_level",
    )
    pension_insured_salary = Column(
        Money,
        nullable=True,
        comment="勞退獨立提繳工資；NULL=沿用 insurance_salary_level",
    )

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


class EmployeeEducation(Base):
    """員工學歷"""

    __tablename__ = "employee_educations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    school_name = Column(String(100), nullable=False, comment="學校名稱")
    major = Column(String(100), nullable=True, comment="科系")
    degree = Column(
        String(20), nullable=False, comment="學位：高中職/學士/碩士/博士/其他"
    )
    graduation_date = Column(Date, nullable=True, comment="畢業日期")
    is_highest = Column(
        Boolean, default=False, nullable=False, comment="是否為最高學歷"
    )
    remark = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    employee = relationship("Employee", backref="educations")


class EmployeeCertificate(Base):
    """員工證照"""

    __tablename__ = "employee_certificates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    certificate_name = Column(String(100), nullable=False, comment="證照名稱")
    issuer = Column(String(100), nullable=True, comment="頒發機構")
    certificate_number = Column(String(100), nullable=True, comment="證照編號")
    issued_date = Column(Date, nullable=True, comment="取得日期")
    expiry_date = Column(Date, nullable=True, comment="到期日（空值代表永久有效）")
    remark = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    employee = relationship("Employee", backref="certificates")


class EmployeeContract(Base):
    """員工合約"""

    __tablename__ = "employee_contracts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contract_type = Column(
        String(20), nullable=False, comment="合約類型：正式/兼職/試用/臨時/續約"
    )
    start_date = Column(Date, nullable=False, comment="合約起始日")
    end_date = Column(Date, nullable=True, comment="合約結束日（可空）")
    salary_at_contract = Column(Money, nullable=True, comment="簽約時薪資")
    remark = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    employee = relationship("Employee", backref="contracts")
