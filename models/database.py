"""
幼稚園考勤薪資系統 - 資料庫模型定義
Database Schema for Kindergarten Payroll System
"""

from sqlalchemy import create_engine, Column, Integer, String, Float, Date, DateTime, Boolean, ForeignKey, Enum, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime, date
import enum

Base = declarative_base()


class EmployeeType(enum.Enum):
    """員工類型"""
    REGULAR = "regular"  # 正職員工
    HOURLY = "hourly"    # 才藝老師 (時薪制)


class AttendanceStatus(enum.Enum):
    """考勤狀態"""
    NORMAL = "normal"           # 正常
    LATE = "late"               # 遲到
    EARLY_LEAVE = "early_leave" # 早退
    MISSING_PUNCH = "missing"   # 未打卡
    ABSENT = "absent"           # 缺勤


class LeaveType(enum.Enum):
    """請假類型"""
    SICK = "sick"               # 病假
    PERSONAL = "personal"       # 事假
    MENSTRUAL = "menstrual"     # 生理假
    ANNUAL = "annual"           # 特休
    MATERNITY = "maternity"     # 產假
    PATERNITY = "paternity"     # 陪產假


class Employee(Base):
    """
    員工表 - 儲存員工基本資料
    """
    __tablename__ = "employees"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(String(20), unique=True, nullable=False, comment="工號")
    name = Column(String(50), nullable=False, comment="姓名")
    id_number = Column(String(20), comment="身分證字號")
    
    # 員工類型
    employee_type = Column(String(20), default=EmployeeType.REGULAR.value, comment="員工類型：regular/hourly")
    
    # 薪資相關
    base_salary = Column(Float, default=0, comment="底薪")
    hourly_rate = Column(Float, default=0, comment="時薪（才藝老師用）")
    
    # 津貼設定
    supervisor_allowance = Column(Float, default=0, comment="主管加給")
    teacher_allowance = Column(Float, default=0, comment="導師津貼")
    meal_allowance = Column(Float, default=0, comment="伙食津貼")
    transportation_allowance = Column(Float, default=0, comment="交通津貼")
    other_allowance = Column(Float, default=0, comment="其他津貼")
    
    # 銀行帳戶
    bank_code = Column(String(10), comment="銀行代碼")
    bank_account = Column(String(30), comment="銀行帳號")
    bank_account_name = Column(String(50), comment="帳戶戶名")
    
    # 勞健保
    insurance_salary_level = Column(Float, default=0, comment="投保薪資級距")
    
    # 工作時間設定
    work_start_time = Column(String(5), default="08:00", comment="上班時間 HH:MM")
    work_end_time = Column(String(5), default="17:00", comment="下班時間 HH:MM")
    
    # 狀態
    is_active = Column(Boolean, default=True, comment="是否在職")
    hire_date = Column(Date, comment="到職日期")
    
    # 時間戳記
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 關聯
    attendances = relationship("Attendance", back_populates="employee")
    leaves = relationship("LeaveRecord", back_populates="employee")
    salaries = relationship("SalaryRecord", back_populates="employee")
    

class Attendance(Base):
    """
    考勤記錄表 - 儲存每日打卡記錄
    """
    __tablename__ = "attendances"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    
    attendance_date = Column(Date, nullable=False, comment="考勤日期")
    punch_in_time = Column(DateTime, comment="上班打卡時間")
    punch_out_time = Column(DateTime, comment="下班打卡時間")
    
    # 異常狀態
    status = Column(String(20), default=AttendanceStatus.NORMAL.value, comment="考勤狀態")
    is_late = Column(Boolean, default=False, comment="是否遲到")
    is_early_leave = Column(Boolean, default=False, comment="是否早退")
    is_missing_punch_in = Column(Boolean, default=False, comment="是否未打卡（上班）")
    is_missing_punch_out = Column(Boolean, default=False, comment="是否未打卡（下班）")
    
    late_minutes = Column(Integer, default=0, comment="遲到分鐘數")
    early_leave_minutes = Column(Integer, default=0, comment="早退分鐘數")
    
    # 備註
    remark = Column(Text, comment="備註")
    
    # 時間戳記
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 關聯
    employee = relationship("Employee", back_populates="attendances")


class LeaveRecord(Base):
    """
    請假記錄表
    """
    __tablename__ = "leave_records"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    
    leave_type = Column(String(20), nullable=False, comment="請假類型")
    start_date = Column(Date, nullable=False, comment="開始日期")
    end_date = Column(Date, nullable=False, comment="結束日期")
    leave_hours = Column(Float, default=8, comment="請假時數")
    
    # 是否扣薪
    is_deductible = Column(Boolean, default=True, comment="是否扣薪")
    deduction_ratio = Column(Float, default=1.0, comment="扣薪比例")
    
    reason = Column(Text, comment="請假原因")
    
    # 審核狀態
    is_approved = Column(Boolean, default=False, comment="是否核准")
    approved_by = Column(String(50), comment="核准人")
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    employee = relationship("Employee", back_populates="leaves")


class SalaryRecord(Base):
    """
    薪資記錄表 - 儲存每月結算結果
    """
    __tablename__ = "salary_records"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    
    # 薪資期間
    salary_year = Column(Integer, nullable=False, comment="年")
    salary_month = Column(Integer, nullable=False, comment="月")
    
    # === 應領項目 ===
    base_salary = Column(Float, default=0, comment="底薪")
    
    # 津貼
    supervisor_allowance = Column(Float, default=0, comment="主管加給")
    teacher_allowance = Column(Float, default=0, comment="導師津貼")
    meal_allowance = Column(Float, default=0, comment="伙食津貼")
    transportation_allowance = Column(Float, default=0, comment="交通津貼")
    other_allowance = Column(Float, default=0, comment="其他津貼")
    
    # 獎金
    festival_bonus = Column(Float, default=0, comment="節慶獎金")
    overtime_bonus = Column(Float, default=0, comment="超額獎金")
    performance_bonus = Column(Float, default=0, comment="績效獎金")
    special_bonus = Column(Float, default=0, comment="特別獎金/紅利")
    
    # 時薪員工專用
    work_hours = Column(Float, default=0, comment="工作時數（時薪制用）")
    hourly_rate = Column(Float, default=0, comment="時薪")
    hourly_total = Column(Float, default=0, comment="時薪總計")
    
    # === 代扣項目 ===
    # 勞健保
    labor_insurance_employee = Column(Float, default=0, comment="勞保費（員工自付）")
    labor_insurance_employer = Column(Float, default=0, comment="勞保費（雇主負擔）")
    health_insurance_employee = Column(Float, default=0, comment="健保費（員工自付）")
    health_insurance_employer = Column(Float, default=0, comment="健保費（雇主負擔）")
    pension_employee = Column(Float, default=0, comment="勞退自提")
    pension_employer = Column(Float, default=0, comment="勞退雇提")
    
    # 考勤扣款
    late_deduction = Column(Float, default=0, comment="遲到扣款")
    early_leave_deduction = Column(Float, default=0, comment="早退扣款")
    missing_punch_deduction = Column(Float, default=0, comment="未打卡扣款")
    leave_deduction = Column(Float, default=0, comment="請假扣款")
    
    # 其他扣款
    other_deduction = Column(Float, default=0, comment="其他扣款")
    
    # 考勤統計
    late_count = Column(Integer, default=0, comment="遲到次數")
    early_leave_count = Column(Integer, default=0, comment="早退次數")
    missing_punch_count = Column(Integer, default=0, comment="未打卡次數")
    
    # === 合計 ===
    gross_salary = Column(Float, default=0, comment="應發總額")
    total_deduction = Column(Float, default=0, comment="扣款總額")
    net_salary = Column(Float, default=0, comment="實發金額")
    
    # 特殊標記
    bonus_separate = Column(Boolean, default=False, comment="獎金是否獨立轉帳")
    bonus_amount = Column(Float, default=0, comment="獨立轉帳獎金金額")
    
    # 備註
    remark = Column(Text, comment="備註")
    
    # 狀態
    is_finalized = Column(Boolean, default=False, comment="是否已結算")
    finalized_at = Column(DateTime, comment="結算時間")
    finalized_by = Column(String(50), comment="結算人")
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    employee = relationship("Employee", back_populates="salaries")


class InsuranceTable(Base):
    """
    勞健保級距表 - 2026年台灣勞健保級距
    """
    __tablename__ = "insurance_tables"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    year = Column(Integer, nullable=False, comment="年度")
    
    # 投保金額範圍
    salary_min = Column(Float, nullable=False, comment="薪資下限")
    salary_max = Column(Float, nullable=False, comment="薪資上限")
    insured_amount = Column(Float, nullable=False, comment="投保金額")
    
    # 勞保費率
    labor_rate_employee = Column(Float, default=0.115, comment="勞保費率-員工")
    labor_rate_employer = Column(Float, default=0.805, comment="勞保費率-雇主")
    
    # 健保費率 (第一類第一目 - 受雇於政府)
    health_rate_employee = Column(Float, default=0.0517, comment="健保費率-員工")
    health_rate_employer = Column(Float, default=0.0517, comment="健保費率-雇主")
    
    # 勞退費率
    pension_rate_employer = Column(Float, default=0.06, comment="勞退費率-雇主提撥")
    
    # 計算結果（方便查詢）
    labor_employee = Column(Float, default=0, comment="勞保費-員工自付")
    labor_employer = Column(Float, default=0, comment="勞保費-雇主負擔")
    health_employee = Column(Float, default=0, comment="健保費-員工自付")
    health_employer = Column(Float, default=0, comment="健保費-雇主負擔")
    pension_employer_amount = Column(Float, default=0, comment="勞退-雇主提撥金額")
    
    created_at = Column(DateTime, default=datetime.now)


class DeductionRule(Base):
    """
    扣款規則表 - 定義各類扣款邏輯
    """
    __tablename__ = "deduction_rules"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_name = Column(String(50), unique=True, nullable=False, comment="規則名稱")
    rule_type = Column(String(20), nullable=False, comment="規則類型: late/early/missing/leave")
    
    # 累計扣款設定
    threshold_count = Column(Integer, default=1, comment="達到幾次開始扣款")
    deduction_per_time = Column(Float, default=0, comment="每次扣款金額")
    
    # 比例扣款設定（用於請假）
    deduction_ratio = Column(Float, default=0, comment="扣款比例")
    
    # 是否啟用
    is_active = Column(Boolean, default=True)
    
    description = Column(Text, comment="規則說明")
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class BonusSetting(Base):
    """
    獎金設定表
    """
    __tablename__ = "bonus_settings"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    setting_year = Column(Integer, nullable=False, comment="年")
    setting_month = Column(Integer, nullable=False, comment="月")
    
    # 節慶獎金設定
    target_enrollment = Column(Integer, default=0, comment="目標人數")
    current_enrollment = Column(Integer, default=0, comment="在籍人數")
    festival_bonus_base = Column(Float, default=0, comment="節慶獎金基數")
    
    # 超額獎金設定
    overtime_threshold = Column(Integer, default=0, comment="超額獎金門檻人數")
    overtime_bonus_per_student = Column(Float, default=0, comment="每超額一人獎金")
    
    # 計算結果
    festival_bonus_ratio = Column(Float, default=0, comment="節慶獎金比率")
    calculated_festival_bonus = Column(Float, default=0, comment="計算後節慶獎金")
    calculated_overtime_bonus = Column(Float, default=0, comment="計算後超額獎金")
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Student(Base):
    """
    學生表 - 儲存學生基本資料
    """
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(String(20), unique=True, nullable=False, comment="學號")
    name = Column(String(50), nullable=False, comment="姓名")
    birthday = Column(Date, nullable=True, comment="生日")
    enrollment_date = Column(Date, nullable=True, comment="入學日期")
    
    # 聯絡資訊
    parent_name = Column(String(50), nullable=True, comment="家長姓名")
    phone = Column(String(20), nullable=True, comment="聯絡電話")
    address = Column(String(100), nullable=True, comment="地址")
    
    # 狀態
    is_active = Column(Boolean, default=True, comment="是否在讀")
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)



# 預設資料庫連線字串 (PostgreSQL)
DEFAULT_DATABASE_URL = "postgresql://yilunwu@localhost:5432/kindergarten_payroll"


# 資料庫初始化函數
def init_database(database_url: str = None):
    """
    初始化資料庫並建立所有表格
    """
    if database_url is None:
        database_url = DEFAULT_DATABASE_URL
    engine = create_engine(database_url, echo=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def get_session(database_url: str = None):
    """
    取得資料庫 session
    """
    if database_url is None:
        database_url = DEFAULT_DATABASE_URL
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)
    return Session()


if __name__ == "__main__":
    # 測試資料庫建立
    engine, Session = init_database()
    print("資料庫初始化完成！")
