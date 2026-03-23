"""
models/salary.py — 薪資相關模型
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, Float, Date, DateTime, Boolean, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from models.base import Base


class InsuranceTable(Base):
    """勞健保級距表"""
    __tablename__ = "insurance_tables"

    id = Column(Integer, primary_key=True, autoincrement=True)
    year = Column(Integer, nullable=False, comment="年度")

    salary_min = Column(Float, nullable=False, comment="薪資下限")
    salary_max = Column(Float, nullable=False, comment="薪資上限")
    insured_amount = Column(Float, nullable=False, comment="投保金額")

    labor_rate_employee = Column(Float, default=0.115)
    labor_rate_employer = Column(Float, default=0.805)
    health_rate_employee = Column(Float, default=0.0517)
    health_rate_employer = Column(Float, default=0.0517)
    pension_rate_employer = Column(Float, default=0.06)

    labor_employee = Column(Float, default=0)
    labor_employer = Column(Float, default=0)
    health_employee = Column(Float, default=0)
    health_employer = Column(Float, default=0)
    pension_employer_amount = Column(Float, default=0)

    created_at = Column(DateTime, default=datetime.now)


class DeductionRule(Base):
    """扣款規則表"""
    __tablename__ = "deduction_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_name = Column(String(50), unique=True, nullable=False)
    rule_type = Column(String(20), nullable=False)

    threshold_count = Column(Integer, default=1)
    deduction_per_time = Column(Float, default=0)
    deduction_ratio = Column(Float, default=0)

    is_active = Column(Boolean, default=True)
    description = Column(Text)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class BonusSetting(Base):
    """獎金設定表"""
    __tablename__ = "bonus_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    setting_year = Column(Integer, nullable=False)
    setting_month = Column(Integer, nullable=False)

    target_enrollment = Column(Integer, default=0)
    current_enrollment = Column(Integer, default=0)
    festival_bonus_base = Column(Float, default=0)

    overtime_threshold = Column(Integer, default=0)
    overtime_bonus_per_student = Column(Float, default=0)

    festival_bonus_ratio = Column(Float, default=0)
    calculated_festival_bonus = Column(Float, default=0)
    calculated_overtime_bonus = Column(Float, default=0)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ClassBonusSetting(Base):
    """班級獎金設定表"""
    __tablename__ = "class_bonus_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=False)

    target_enrollment = Column(Integer, default=0)
    current_enrollment = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.now)


class AllowanceType(Base):
    """津貼類型表"""
    __tablename__ = "allowance_types"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(30), unique=True, nullable=False)
    name = Column(String(50), nullable=False)
    description = Column(String(200))
    is_taxable = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class DeductionType(Base):
    """扣款類型表"""
    __tablename__ = "deduction_types"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(30), unique=True, nullable=False)
    name = Column(String(50), nullable=False)
    description = Column(String(200))
    category = Column(String(20), default='other')
    is_employer_paid = Column(Boolean, default=False)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class BonusType(Base):
    """獎金類型表"""
    __tablename__ = "bonus_types"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(30), unique=True, nullable=False)
    name = Column(String(50), nullable=False)
    description = Column(String(200))
    is_separate_transfer = Column(Boolean, default=False)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class EmployeeAllowance(Base):
    """員工津貼設定表"""
    __tablename__ = "employee_allowances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    allowance_type_id = Column(Integer, ForeignKey("allowance_types.id"), nullable=False)
    amount = Column(Float, default=0)
    effective_date = Column(Date)
    end_date = Column(Date)
    remark = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class SalaryItem(Base):
    """薪資明細項目表"""
    __tablename__ = "salary_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    salary_record_id = Column(Integer, ForeignKey("salary_records.id", ondelete="CASCADE"), nullable=False)
    item_category = Column(String(20), nullable=False)
    item_type_id = Column(Integer, nullable=False)
    item_code = Column(String(30), nullable=False)
    item_name = Column(String(50), nullable=False)
    amount = Column(Float, default=0)
    quantity = Column(Integer, default=1)
    unit_amount = Column(Float)
    is_employer_paid = Column(Boolean, default=False)
    remark = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class SalaryRecord(Base):
    """薪資記錄表"""
    __tablename__ = "salary_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    # 計算時使用的設定版本 FK（用於稽核追蹤）
    bonus_config_id = Column(Integer, ForeignKey("bonus_configs.id"), nullable=True, comment="計算時使用的獎金設定版本")
    attendance_policy_id = Column(Integer, ForeignKey("attendance_policies.id"), nullable=True, comment="計算時使用的考勤政策版本")

    salary_year = Column(Integer, nullable=False, comment="年")
    salary_month = Column(Integer, nullable=False, comment="月")

    base_salary = Column(Float, default=0, comment="底薪")

    supervisor_allowance = Column(Float, default=0, comment="主管加給")
    teacher_allowance = Column(Float, default=0, comment="導師津貼")
    meal_allowance = Column(Float, default=0, comment="伙食津貼")
    transportation_allowance = Column(Float, default=0, comment="交通津貼")
    other_allowance = Column(Float, default=0, comment="其他津貼")

    festival_bonus = Column(Float, default=0, comment="節慶獎金")
    overtime_bonus = Column(Float, default=0, comment="超額獎金")
    performance_bonus = Column(Float, default=0, comment="績效獎金")
    special_bonus = Column(Float, default=0, comment="特別獎金/紅利")

    overtime_pay = Column(Float, default=0, comment="加班費")
    meeting_overtime_pay = Column(Float, default=0, comment="園務會議加班費")
    meeting_absence_deduction = Column(Float, default=0, comment="園務會議缺席扣節慶獎金")
    birthday_bonus = Column(Float, default=0, comment="生日禮金")

    work_hours = Column(Float, default=0, comment="工作時數（時薪制用）")
    hourly_rate = Column(Float, default=0, comment="時薪")
    hourly_total = Column(Float, default=0, comment="時薪總計")

    labor_insurance_employee = Column(Float, default=0, comment="勞保費（員工自付）")
    labor_insurance_employer = Column(Float, default=0, comment="勞保費（雇主負擔）")
    health_insurance_employee = Column(Float, default=0, comment="健保費（員工自付）")
    health_insurance_employer = Column(Float, default=0, comment="健保費（雇主負擔）")
    pension_employee = Column(Float, default=0, comment="勞退自提")
    pension_employer = Column(Float, default=0, comment="勞退雇提")

    late_deduction = Column(Float, default=0, comment="遲到扣款")
    early_leave_deduction = Column(Float, default=0, comment="早退扣款")
    missing_punch_deduction = Column(Float, default=0, comment="未打卡扣款")
    leave_deduction = Column(Float, default=0, comment="請假扣款")
    absence_deduction = Column(Float, default=0, comment="曠職扣款")
    other_deduction = Column(Float, default=0, comment="其他扣款")

    late_count = Column(Integer, default=0, comment="遲到次數")
    early_leave_count = Column(Integer, default=0, comment="早退次數")
    missing_punch_count = Column(Integer, default=0, comment="未打卡次數")
    absent_count = Column(Integer, default=0, comment="曠職天數")

    gross_salary = Column(Float, default=0, comment="應發總額")
    total_deduction = Column(Float, default=0, comment="扣款總額")
    net_salary = Column(Float, default=0, comment="實發金額")

    bonus_separate = Column(Boolean, default=False, comment="獎金是否獨立轉帳")
    bonus_amount = Column(Float, default=0, comment="獨立轉帳獎金金額（festival+overtime+supervisor_dividend）")
    supervisor_dividend = Column(Float, default=0, comment="主管紅利（獨立轉帳）")

    remark = Column(Text, comment="備註")

    is_finalized = Column(Boolean, default=False, comment="是否已結算")
    finalized_at = Column(DateTime, comment="結算時間")
    finalized_by = Column(String(50), comment="結算人")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint('employee_id', 'salary_year', 'salary_month', name='uq_salary_emp_ym'),
        Index('ix_salary_emp_ym_finalized', 'employee_id', 'salary_year', 'salary_month', 'is_finalized'),
        Index('ix_salary_ym_finalized', 'salary_year', 'salary_month', 'is_finalized'),
        Index('ix_salary_bonus_config_id', 'bonus_config_id'),
        Index('ix_salary_attendance_policy_id', 'attendance_policy_id'),
    )

    employee = relationship("Employee", back_populates="salaries")
