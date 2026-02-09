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
    title = Column(String(50), nullable=True, comment="職稱 (Legacy)")
    job_title_id = Column(Integer, ForeignKey("job_titles.id"), nullable=True, comment="職稱 ID")
    position = Column(String(50), nullable=True, comment="職務 (Duty)")
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=True, comment="所屬班級")
    
    # 關聯
    job_title_rel = relationship("JobTitle", backref="employees")
    
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
    is_office_staff = Column(Boolean, default=False, comment="是否為辦公室人員（超額獎金用全校計算）")
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


class ClassBonusSetting(Base):
    """
    班級獎金設定表 - 紀錄各班級每月的目標與人數
    """
    __tablename__ = "class_bonus_settings"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    year = Column(Integer, nullable=False, comment="年")
    month = Column(Integer, nullable=False, comment="月")
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=False, comment="班級ID")
    
    target_enrollment = Column(Integer, default=0, comment="目標人數")
    current_enrollment = Column(Integer, default=0, comment="在籍人數")
    
    created_at = Column(DateTime, default=datetime.now)



class Student(Base):
    """
    學生表 - 儲存學生基本資料
    """
    __tablename__ = "students"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(String(20), unique=True, nullable=False, comment="學號")
    name = Column(String(50), nullable=False, comment="姓名")
    gender = Column(String(10), nullable=True, comment="性別")
    birthday = Column(Date, nullable=True, comment="生日")
    classroom_id = Column(Integer, ForeignKey("classrooms.id"), nullable=True, comment="班級")
    enrollment_date = Column(Date, nullable=True, comment="入學日期")
    graduation_date = Column(Date, nullable=True, comment="畢業日期")
    status = Column(String(20), nullable=True, comment="狀態")

    # 聯絡資訊
    parent_name = Column(String(50), nullable=True, comment="家長姓名")
    parent_phone = Column(String(20), nullable=True, comment="聯絡電話")
    address = Column(String(200), nullable=True, comment="地址")
    notes = Column(Text, nullable=True, comment="備註")

    # 狀態
    is_active = Column(Boolean, default=True, comment="是否在讀")
    status_tag = Column(String(50), nullable=True, comment="狀態標籤 (新生/不足齡/特殊生等)")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ClassGrade(Base):
    """
    年級表 - 大班、中班、小班、幼幼班
    """
    __tablename__ = "class_grades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False, comment="年級名稱")
    age_range = Column(String(20), nullable=True, comment="年齡範圍")
    sort_order = Column(Integer, default=0, comment="排序")
    is_active = Column(Boolean, default=True, comment="是否啟用")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class Classroom(Base):
    """
    班級表 - 儲存班級資料
    """
    __tablename__ = "classrooms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False, comment="班級名稱")
    grade_id = Column(Integer, ForeignKey("class_grades.id"), nullable=True, comment="年級")
    capacity = Column(Integer, default=30, comment="班級容量")
    current_count = Column(Integer, default=0, comment="目前人數")

    # 老師
    head_teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True, comment="班導師")
    assistant_teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True, comment="副班導")
    art_teacher_id = Column(Integer, ForeignKey("employees.id"), nullable=True, comment="美師")
    
    class_code = Column(String(20), nullable=True, comment="班級代號 (如 114-11)")

    is_active = Column(Boolean, default=True, comment="是否啟用")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# ============================================
# 第三正規化：類型表
# ============================================

class AllowanceType(Base):
    """津貼類型表"""
    __tablename__ = "allowance_types"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(30), unique=True, nullable=False, comment="代碼")
    name = Column(String(50), nullable=False, comment="名稱")
    description = Column(String(200), comment="說明")
    is_taxable = Column(Boolean, default=True, comment="是否課稅")
    sort_order = Column(Integer, default=0, comment="排序")
    is_active = Column(Boolean, default=True, comment="是否啟用")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class DeductionType(Base):
    """扣款類型表"""
    __tablename__ = "deduction_types"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(30), unique=True, nullable=False, comment="代碼")
    name = Column(String(50), nullable=False, comment="名稱")
    description = Column(String(200), comment="說明")
    category = Column(String(20), default='other', comment="分類: insurance/attendance/leave/other")
    is_employer_paid = Column(Boolean, default=False, comment="是否雇主負擔")
    sort_order = Column(Integer, default=0, comment="排序")
    is_active = Column(Boolean, default=True, comment="是否啟用")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class BonusType(Base):
    """獎金類型表"""
    __tablename__ = "bonus_types"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(30), unique=True, nullable=False, comment="代碼")
    name = Column(String(50), nullable=False, comment="名稱")
    description = Column(String(200), comment="說明")
    is_separate_transfer = Column(Boolean, default=False, comment="是否獨立轉帳")
    sort_order = Column(Integer, default=0, comment="排序")
    is_active = Column(Boolean, default=True, comment="是否啟用")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class EmployeeAllowance(Base):
    """員工津貼設定表 (正規化後)"""
    __tablename__ = "employee_allowances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    allowance_type_id = Column(Integer, ForeignKey("allowance_types.id"), nullable=False)
    amount = Column(Float, default=0, comment="金額")
    effective_date = Column(Date, comment="生效日期")
    end_date = Column(Date, comment="結束日期")
    remark = Column(Text, comment="備註")
    is_active = Column(Boolean, default=True, comment="是否啟用")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class SalaryItem(Base):
    """薪資明細項目表 (正規化後)"""
    __tablename__ = "salary_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    salary_record_id = Column(Integer, ForeignKey("salary_records.id", ondelete="CASCADE"), nullable=False)
    item_category = Column(String(20), nullable=False, comment="分類: allowance/deduction/bonus")
    item_type_id = Column(Integer, nullable=False, comment="類型ID")
    item_code = Column(String(30), nullable=False, comment="類型代碼")
    item_name = Column(String(50), nullable=False, comment="項目名稱")
    amount = Column(Float, default=0, comment="金額")
    quantity = Column(Integer, default=1, comment="數量")
    unit_amount = Column(Float, comment="單位金額")
    is_employer_paid = Column(Boolean, default=False, comment="是否雇主負擔")
    remark = Column(Text, comment="備註")
    created_at = Column(DateTime, default=datetime.now)


class JobTitle(Base):
    __tablename__ = "job_titles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)


class SystemConfig(Base):
    """
    系統設定表 - 存儲所有可配置的設定值
    """
    __tablename__ = "system_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_key = Column(String(100), unique=True, nullable=False, comment="設定鍵名")
    config_value = Column(Text, nullable=False, comment="設定值 (JSON)")
    config_type = Column(String(50), default="general", comment="設定類別")
    description = Column(String(200), comment="設定說明")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AttendancePolicy(Base):
    """
    考勤政策表 - 存儲考勤扣款規則
    """
    __tablename__ = "attendance_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 工作時間
    default_work_start = Column(String(5), default="08:00", comment="預設上班時間")
    default_work_end = Column(String(5), default="17:00", comment="預設下班時間")
    grace_minutes = Column(Integer, default=5, comment="遲到寬限時間(分)")

    # 扣款規則
    late_threshold = Column(Integer, default=2, comment="遲到幾次開始扣款")
    late_deduction = Column(Float, default=50, comment="遲到每次扣款金額")
    early_leave_deduction = Column(Float, default=50, comment="早退每次扣款金額")
    missing_punch_deduction = Column(Float, default=50, comment="未打卡每次扣款金額")

    # 節慶獎金資格
    festival_bonus_months = Column(Integer, default=3, comment="入職滿幾個月可領節慶獎金")

    is_active = Column(Boolean, default=True, comment="是否為目前使用的政策")
    effective_date = Column(Date, comment="生效日期")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class BonusConfig(Base):
    """
    獎金設定表 - 存儲節慶獎金、超額獎金、主管紅利等設定
    """
    __tablename__ = "bonus_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_year = Column(Integer, nullable=False, comment="年度")

    # === 節慶獎金基數 ===
    # 班導
    head_teacher_ab = Column(Float, default=2000, comment="班導A/B級獎金基數")
    head_teacher_c = Column(Float, default=1500, comment="班導C級獎金基數")
    # 副班導
    assistant_teacher_ab = Column(Float, default=1200, comment="副班導A/B級獎金基數")
    assistant_teacher_c = Column(Float, default=1200, comment="副班導C級獎金基數")

    # 主管節慶獎金
    principal_festival = Column(Float, default=6500, comment="園長節慶獎金")
    director_festival = Column(Float, default=3500, comment="主任節慶獎金")
    leader_festival = Column(Float, default=2000, comment="組長節慶獎金")

    # 司機/美編/行政節慶獎金
    driver_festival = Column(Float, default=1000, comment="司機節慶獎金基數")
    designer_festival = Column(Float, default=1000, comment="美編節慶獎金基數")
    admin_festival = Column(Float, default=2000, comment="行政節慶獎金基數")

    # === 主管紅利 ===
    principal_dividend = Column(Float, default=5000, comment="園長紅利")
    director_dividend = Column(Float, default=4000, comment="主任紅利")
    leader_dividend = Column(Float, default=3000, comment="組長紅利")
    vice_leader_dividend = Column(Float, default=1500, comment="副組長紅利")

    # === 超額獎金每人金額 ===
    overtime_head_normal = Column(Float, default=400, comment="班導超額每人(大中小班)")
    overtime_head_baby = Column(Float, default=450, comment="班導超額每人(幼幼班)")
    overtime_assistant_normal = Column(Float, default=100, comment="副班導超額每人(大中小班)")
    overtime_assistant_baby = Column(Float, default=150, comment="副班導超額每人(幼幼班)")

    # === 全校目標 ===
    school_wide_target = Column(Integer, default=160, comment="全校目標人數")

    is_active = Column(Boolean, default=True, comment="是否為目前使用的設定")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class GradeTarget(Base):
    """
    年級目標人數表 - 存儲各年級的節慶獎金和超額獎金目標人數
    """
    __tablename__ = "grade_targets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_year = Column(Integer, nullable=False, comment="年度")
    grade_name = Column(String(20), nullable=False, comment="年級名稱")

    # 節慶獎金目標人數
    festival_two_teachers = Column(Integer, default=0, comment="節慶-2老師目標")
    festival_one_teacher = Column(Integer, default=0, comment="節慶-1老師目標")
    festival_shared = Column(Integer, default=0, comment="節慶-共用副班導目標")

    # 超額獎金目標人數
    overtime_two_teachers = Column(Integer, default=0, comment="超額-2老師目標")
    overtime_one_teacher = Column(Integer, default=0, comment="超額-1老師目標")
    overtime_shared = Column(Integer, default=0, comment="超額-共用副班導目標")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class InsuranceRate(Base):
    """
    勞健保費率表
    """
    __tablename__ = "insurance_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rate_year = Column(Integer, nullable=False, comment="年度")

    # 勞保費率
    labor_rate = Column(Float, default=0.12, comment="勞保費率 12%")
    labor_employee_ratio = Column(Float, default=0.20, comment="勞保員工負擔比例 20%")
    labor_employer_ratio = Column(Float, default=0.70, comment="勞保雇主負擔比例 70%")
    labor_government_ratio = Column(Float, default=0.10, comment="勞保政府負擔比例 10%")

    # 健保費率
    health_rate = Column(Float, default=0.0517, comment="健保費率 5.17%")
    health_employee_ratio = Column(Float, default=0.30, comment="健保員工負擔比例 30%")
    health_employer_ratio = Column(Float, default=0.60, comment="健保員工負擔比例 60%")

    # 退休金
    pension_employer_rate = Column(Float, default=0.06, comment="退休金雇主提撥比例 6%")

    # 平均眷數
    average_dependents = Column(Float, default=0.57, comment="平均眷數")

    is_active = Column(Boolean, default=True, comment="是否為目前使用的費率")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# 預設資料庫連線字串 (PostgreSQL)
DEFAULT_DATABASE_URL = "postgresql://yilunwu@localhost:5432/ivymanagement"


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
