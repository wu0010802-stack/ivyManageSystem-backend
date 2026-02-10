"""
幼稚園考勤薪資系統 - 資料庫模型定義
Database Schema for Kindergarten Payroll System
"""

import os
import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, Column, Integer, String, Float, Date, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import enum

logger = logging.getLogger(__name__)

Base = declarative_base()

# ---------------------------------------------------------------------------
# 資料庫連線管理
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://yilunwu@localhost:5432/ivymanagement",
)

_engine = None
_SessionFactory = None


def get_engine():
    """取得全域 Engine（含連線池），只建立一次"""
    global _engine
    if _engine is None:
        _engine = create_engine(
            DATABASE_URL,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            echo=False,
        )
    return _engine


def get_session_factory():
    """取得 SessionFactory，只建立一次"""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine())
    return _SessionFactory


def get_session():
    """取得資料庫 session（向下相容）"""
    return get_session_factory()()


@contextmanager
def session_scope():
    """提供 context manager 風格的 session 管理，自動 commit/rollback/close"""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_database():
    """初始化資料庫並建立所有表格"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    logger.info("資料庫初始化完成")
    return engine, get_session_factory()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EmployeeType(enum.Enum):
    """員工類型"""
    REGULAR = "regular"  # 正職員工
    HOURLY = "hourly"    # 才藝老師 (時薪制)


class AttendanceStatus(enum.Enum):
    """考勤狀態"""
    NORMAL = "normal"
    LATE = "late"
    EARLY_LEAVE = "early_leave"
    MISSING_PUNCH = "missing"
    ABSENT = "absent"


class LeaveType(enum.Enum):
    """請假類型"""
    SICK = "sick"
    PERSONAL = "personal"
    MENSTRUAL = "menstrual"
    ANNUAL = "annual"
    MATERNITY = "maternity"
    PATERNITY = "paternity"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Employee(Base):
    """員工表"""
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(String(20), unique=True, nullable=False, comment="工號")
    name = Column(String(50), nullable=False, comment="姓名")
    id_number = Column(String(20), comment="身分證字號")

    employee_type = Column(String(20), default=EmployeeType.REGULAR.value, comment="員工類型：regular/hourly")
    title = Column(String(50), nullable=True, comment="職稱 (Legacy)")
    job_title_id = Column(Integer, ForeignKey("job_titles.id"), nullable=True, comment="職稱 ID")
    position = Column(String(50), nullable=True, comment="職務 (Duty)")
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=True, comment="所屬班級")

    job_title_rel = relationship("JobTitle", backref="employees")

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

    work_start_time = Column(String(5), default="08:00", comment="上班時間 HH:MM")
    work_end_time = Column(String(5), default="17:00", comment="下班時間 HH:MM")

    is_active = Column(Boolean, default=True, comment="是否在職")
    is_office_staff = Column(Boolean, default=False, comment="是否為辦公室人員")
    dependents = Column(Integer, default=0, comment="眷屬人數（健保計算用）")
    hire_date = Column(Date, comment="到職日期")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    attendances = relationship("Attendance", back_populates="employee", cascade="all, delete-orphan")
    leaves = relationship("LeaveRecord", back_populates="employee", cascade="all, delete-orphan")
    salaries = relationship("SalaryRecord", back_populates="employee", cascade="all, delete-orphan")


class Attendance(Base):
    """考勤記錄表"""
    __tablename__ = "attendances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    attendance_date = Column(Date, nullable=False, comment="考勤日期")
    punch_in_time = Column(DateTime, comment="上班打卡時間")
    punch_out_time = Column(DateTime, comment="下班打卡時間")

    status = Column(String(20), default=AttendanceStatus.NORMAL.value, comment="考勤狀態")
    is_late = Column(Boolean, default=False, comment="是否遲到")
    is_early_leave = Column(Boolean, default=False, comment="是否早退")
    is_missing_punch_in = Column(Boolean, default=False, comment="是否未打卡（上班）")
    is_missing_punch_out = Column(Boolean, default=False, comment="是否未打卡（下班）")

    late_minutes = Column(Integer, default=0, comment="遲到分鐘數")
    early_leave_minutes = Column(Integer, default=0, comment="早退分鐘數")

    remark = Column(Text, comment="備註")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    employee = relationship("Employee", back_populates="attendances")


class LeaveRecord(Base):
    """請假記錄表"""
    __tablename__ = "leave_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    leave_type = Column(String(20), nullable=False, comment="請假類型")
    start_date = Column(Date, nullable=False, comment="開始日期")
    end_date = Column(Date, nullable=False, comment="結束日期")
    leave_hours = Column(Float, default=8, comment="請假時數")

    is_deductible = Column(Boolean, default=True, comment="是否扣薪")
    deduction_ratio = Column(Float, default=1.0, comment="扣薪比例")

    reason = Column(Text, comment="請假原因")

    is_approved = Column(Boolean, default=False, comment="是否核准")
    approved_by = Column(String(50), comment="核准人")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    employee = relationship("Employee", back_populates="leaves")


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

    employee = relationship("Employee", backref="overtimes")


class SalaryRecord(Base):
    """薪資記錄表"""
    __tablename__ = "salary_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

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
    other_deduction = Column(Float, default=0, comment="其他扣款")

    late_count = Column(Integer, default=0, comment="遲到次數")
    early_leave_count = Column(Integer, default=0, comment="早退次數")
    missing_punch_count = Column(Integer, default=0, comment="未打卡次數")

    gross_salary = Column(Float, default=0, comment="應發總額")
    total_deduction = Column(Float, default=0, comment="扣款總額")
    net_salary = Column(Float, default=0, comment="實發金額")

    bonus_separate = Column(Boolean, default=False, comment="獎金是否獨立轉帳")
    bonus_amount = Column(Float, default=0, comment="獨立轉帳獎金金額")

    remark = Column(Text, comment="備註")

    is_finalized = Column(Boolean, default=False, comment="是否已結算")
    finalized_at = Column(DateTime, comment="結算時間")
    finalized_by = Column(String(50), comment="結算人")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    employee = relationship("Employee", back_populates="salaries")


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


class Student(Base):
    """學生表"""
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(String(20), unique=True, nullable=False, comment="學號")
    name = Column(String(50), nullable=False, comment="姓名")
    gender = Column(String(10), nullable=True)
    birthday = Column(Date, nullable=True)
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=True)
    enrollment_date = Column(Date, nullable=True)
    graduation_date = Column(Date, nullable=True)
    status = Column(String(20), nullable=True)

    parent_name = Column(String(50), nullable=True)
    parent_phone = Column(String(20), nullable=True)
    address = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)

    is_active = Column(Boolean, default=True)
    status_tag = Column(String(50), nullable=True, comment="狀態標籤")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ClassGrade(Base):
    """年級表"""
    __tablename__ = "class_grades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False)
    age_range = Column(String(20), nullable=True)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Classroom(Base):
    """班級表"""
    __tablename__ = "classrooms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False)
    grade_id = Column(Integer, ForeignKey("class_grades.id"), nullable=True)
    capacity = Column(Integer, default=30)
    current_count = Column(Integer, default=0)

    head_teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    assistant_teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    art_teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True)

    class_code = Column(String(20), nullable=True, comment="班級代號")

    is_active = Column(Boolean, default=True)

    grade = relationship("ClassGrade")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# ---------------------------------------------------------------------------
# 第三正規化：類型表
# ---------------------------------------------------------------------------

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
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
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


class JobTitle(Base):
    __tablename__ = "job_titles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)


class SystemConfig(Base):
    """系統設定表"""
    __tablename__ = "system_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_key = Column(String(100), unique=True, nullable=False)
    config_value = Column(Text, nullable=False)
    config_type = Column(String(50), default="general")
    description = Column(String(200))

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AttendancePolicy(Base):
    """考勤政策表"""
    __tablename__ = "attendance_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)

    default_work_start = Column(String(5), default="08:00")
    default_work_end = Column(String(5), default="17:00")
    grace_minutes = Column(Integer, default=5)

    late_threshold = Column(Integer, default=2)
    late_deduction = Column(Float, default=50)
    early_leave_deduction = Column(Float, default=50)
    missing_punch_deduction = Column(Float, default=50)

    festival_bonus_months = Column(Integer, default=3)

    is_active = Column(Boolean, default=True)
    effective_date = Column(Date)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class BonusConfig(Base):
    """獎金設定表"""
    __tablename__ = "bonus_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_year = Column(Integer, nullable=False)

    head_teacher_ab = Column(Float, default=2000)
    head_teacher_c = Column(Float, default=1500)
    assistant_teacher_ab = Column(Float, default=1200)
    assistant_teacher_c = Column(Float, default=1200)

    principal_festival = Column(Float, default=6500)
    director_festival = Column(Float, default=3500)
    leader_festival = Column(Float, default=2000)

    driver_festival = Column(Float, default=1000)
    designer_festival = Column(Float, default=1000)
    admin_festival = Column(Float, default=2000)

    principal_dividend = Column(Float, default=5000)
    director_dividend = Column(Float, default=4000)
    leader_dividend = Column(Float, default=3000)
    vice_leader_dividend = Column(Float, default=1500)

    overtime_head_normal = Column(Float, default=400)
    overtime_head_baby = Column(Float, default=450)
    overtime_assistant_normal = Column(Float, default=100)
    overtime_assistant_baby = Column(Float, default=150)

    school_wide_target = Column(Integer, default=160)

    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class GradeTarget(Base):
    """年級目標人數表"""
    __tablename__ = "grade_targets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_year = Column(Integer, nullable=False)
    grade_name = Column(String(20), nullable=False)

    festival_two_teachers = Column(Integer, default=0)
    festival_one_teacher = Column(Integer, default=0)
    festival_shared = Column(Integer, default=0)

    overtime_two_teachers = Column(Integer, default=0)
    overtime_one_teacher = Column(Integer, default=0)
    overtime_shared = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class InsuranceRate(Base):
    """勞健保費率表"""
    __tablename__ = "insurance_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rate_year = Column(Integer, nullable=False)

    labor_rate = Column(Float, default=0.12)
    labor_employee_ratio = Column(Float, default=0.20)
    labor_employer_ratio = Column(Float, default=0.70)
    labor_government_ratio = Column(Float, default=0.10)

    health_rate = Column(Float, default=0.0517)
    health_employee_ratio = Column(Float, default=0.30)
    health_employer_ratio = Column(Float, default=0.60)

    pension_employer_rate = Column(Float, default=0.06)

    average_dependents = Column(Float, default=0.57)

    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class User(Base):
    """用戶認證表"""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), unique=True, nullable=False, comment="關聯員工ID")
    username = Column(String(50), unique=True, nullable=False, comment="登入帳號")
    password_hash = Column(String(255), nullable=False, comment="密碼雜湊")
    role = Column(String(20), default="teacher", comment="角色: teacher/admin")
    is_active = Column(Boolean, default=True, comment="帳號是否啟用")
    last_login = Column(DateTime, comment="最後登入時間")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    employee = relationship("Employee", backref="user_account")


if __name__ == "__main__":
    engine, Session = init_database()
    print("資料庫初始化完成！")
