"""
models/salary.py — 薪資相關模型
"""

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
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from models.base import Base
from models.types import Money


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


class DeductionType(Base):
    """扣款類型表"""

    __tablename__ = "deduction_types"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(30), unique=True, nullable=False)
    name = Column(String(50), nullable=False)
    description = Column(String(200))
    category = Column(String(20), default="other")
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


class SalaryItem(Base):
    """薪資明細項目表"""

    __tablename__ = "salary_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    salary_record_id = Column(
        Integer, ForeignKey("salary_records.id", ondelete="CASCADE"), nullable=False
    )
    item_category = Column(String(20), nullable=False)
    item_type_id = Column(Integer, nullable=False)
    item_code = Column(String(30), nullable=False)
    item_name = Column(String(50), nullable=False)
    amount = Column(Money, default=0)
    quantity = Column(Integer, default=1)
    unit_amount = Column(Money)
    is_employer_paid = Column(Boolean, default=False)
    remark = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


class SalaryRecord(Base):
    """薪資記錄表"""

    __tablename__ = "salary_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)

    # 計算時使用的設定版本 FK（用於稽核追蹤）
    bonus_config_id = Column(
        Integer,
        ForeignKey("bonus_configs.id"),
        nullable=True,
        comment="計算時使用的獎金設定版本",
    )
    attendance_policy_id = Column(
        Integer,
        ForeignKey("attendance_policies.id"),
        nullable=True,
        comment="計算時使用的考勤政策版本",
    )

    salary_year = Column(Integer, nullable=False, comment="年")
    salary_month = Column(Integer, nullable=False, comment="月")

    base_salary = Column(Money, default=0, comment="底薪")

    festival_bonus = Column(Money, default=0, comment="節慶獎金")
    overtime_bonus = Column(Money, default=0, comment="超額獎金")
    performance_bonus = Column(Money, default=0, comment="績效獎金")
    special_bonus = Column(Money, default=0, comment="特別獎金/紅利")

    overtime_pay = Column(Money, default=0, comment="加班費")
    meeting_overtime_pay = Column(Money, default=0, comment="園務會議加班費")
    meeting_absence_deduction = Column(
        Money, default=0, comment="園務會議缺席扣節慶獎金"
    )
    birthday_bonus = Column(Money, default=0, comment="生日禮金")

    work_hours = Column(Float, default=0, comment="工作時數（時薪制用）")
    hourly_rate = Column(Money, default=0, comment="時薪")
    hourly_total = Column(Money, default=0, comment="時薪總計")

    labor_insurance_employee = Column(Money, default=0, comment="勞保費（員工自付）")
    labor_insurance_employer = Column(Money, default=0, comment="勞保費（雇主負擔）")
    health_insurance_employee = Column(Money, default=0, comment="健保費（員工自付）")
    health_insurance_employer = Column(Money, default=0, comment="健保費（雇主負擔）")
    pension_employee = Column(Money, default=0, comment="勞退自提")
    pension_employer = Column(Money, default=0, comment="勞退雇提")

    late_deduction = Column(Money, default=0, comment="遲到扣款")
    early_leave_deduction = Column(Money, default=0, comment="早退扣款")
    missing_punch_deduction = Column(Money, default=0, comment="未打卡扣款")
    leave_deduction = Column(Money, default=0, comment="請假扣款")
    absence_deduction = Column(Money, default=0, comment="曠職扣款")
    other_deduction = Column(Money, default=0, comment="其他扣款")

    late_count = Column(Integer, default=0, comment="遲到次數")
    early_leave_count = Column(Integer, default=0, comment="早退次數")
    missing_punch_count = Column(Integer, default=0, comment="未打卡次數")
    absent_count = Column(Integer, default=0, comment="曠職天數")

    gross_salary = Column(Money, default=0, comment="應發總額")
    total_deduction = Column(Money, default=0, comment="扣款總額")
    net_salary = Column(Money, default=0, comment="實發金額")

    bonus_separate = Column(Boolean, default=False, comment="獎金是否獨立轉帳")
    bonus_amount = Column(
        Money,
        default=0,
        comment="獨立轉帳獎金金額（festival+overtime+supervisor_dividend）",
    )
    supervisor_dividend = Column(Money, default=0, comment="主管紅利（獨立轉帳）")

    remark = Column(Text, comment="備註")

    is_finalized = Column(Boolean, default=False, comment="是否已結算")
    finalized_at = Column(DateTime, comment="結算時間")
    finalized_by = Column(String(50), comment="結算人")

    version = Column(
        Integer, nullable=False, default=1, server_default="1", comment="樂觀鎖版本號"
    )

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            "employee_id", "salary_year", "salary_month", name="uq_salary_emp_ym"
        ),
        Index(
            "ix_salary_emp_ym_finalized",
            "employee_id",
            "salary_year",
            "salary_month",
            "is_finalized",
        ),
        Index("ix_salary_ym_finalized", "salary_year", "salary_month", "is_finalized"),
        Index("ix_salary_bonus_config_id", "bonus_config_id"),
        Index("ix_salary_attendance_policy_id", "attendance_policy_id"),
    )

    employee = relationship("Employee", back_populates="salaries")


class SalarySnapshot(Base):
    """薪資快照表 — 不可變歷史

    與 SalaryRecord 分層：
    - SalaryRecord 為「可變工作副本」，每次重算會 UPDATE 覆蓋
    - SalarySnapshot 為「不可變歷史」，捕捉特定時間點的薪資狀態

    快照類型（snapshot_type）：
    - month_end：月底自動快照（Lazy + 排程雙保險觸發）
    - finalize：封存整月時同步寫入（即使後續解封仍保留）
    - manual：管理員手動補拍（可填 snapshot_remark）

    金額欄位結構與 SalaryRecord 保持一致，方便反射複製；
    新增欄位至 SalaryRecord 時須同步於此補上對應欄位（PR checklist 提醒）。
    """

    __tablename__ = "salary_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    salary_record_id = Column(
        Integer,
        ForeignKey("salary_records.id", ondelete="SET NULL"),
        nullable=True,
        comment="來源 SalaryRecord；record 被刪仍保留快照",
    )
    employee_id = Column(
        Integer,
        nullable=False,
        comment="冗餘，用於個人歷史查詢；不設 FK 避免員工刪除連動",
    )
    salary_year = Column(Integer, nullable=False)
    salary_month = Column(Integer, nullable=False)

    # ── 設定版本 FK（與 SalaryRecord 同步，確保快照可獨立稽核） ───────────
    bonus_config_id = Column(
        Integer,
        ForeignKey("bonus_configs.id", ondelete="SET NULL"),
        nullable=True,
        comment="拍攝當下的獎金設定版本；與 SalaryRecord.bonus_config_id 對齊",
    )
    attendance_policy_id = Column(
        Integer,
        ForeignKey("attendance_policies.id", ondelete="SET NULL"),
        nullable=True,
        comment="拍攝當下的考勤政策版本；與 SalaryRecord.attendance_policy_id 對齊",
    )

    # ── 以下為 SalaryRecord 金額/計數/布林/備註欄位完整複製 ───────────────────
    base_salary = Column(Money, default=0)
    festival_bonus = Column(Money, default=0)
    overtime_bonus = Column(Money, default=0)
    performance_bonus = Column(Money, default=0)
    special_bonus = Column(Money, default=0)
    overtime_pay = Column(Money, default=0)
    meeting_overtime_pay = Column(Money, default=0)
    meeting_absence_deduction = Column(Money, default=0)
    birthday_bonus = Column(Money, default=0)
    work_hours = Column(Float, default=0)
    hourly_rate = Column(Money, default=0)
    hourly_total = Column(Money, default=0)
    labor_insurance_employee = Column(Money, default=0)
    labor_insurance_employer = Column(Money, default=0)
    health_insurance_employee = Column(Money, default=0)
    health_insurance_employer = Column(Money, default=0)
    pension_employee = Column(Money, default=0)
    pension_employer = Column(Money, default=0)
    late_deduction = Column(Money, default=0)
    early_leave_deduction = Column(Money, default=0)
    missing_punch_deduction = Column(Money, default=0)
    leave_deduction = Column(Money, default=0)
    absence_deduction = Column(Money, default=0)
    other_deduction = Column(Money, default=0)
    late_count = Column(Integer, default=0)
    early_leave_count = Column(Integer, default=0)
    missing_punch_count = Column(Integer, default=0)
    absent_count = Column(Integer, default=0)
    gross_salary = Column(Money, default=0)
    total_deduction = Column(Money, default=0)
    net_salary = Column(Money, default=0)
    bonus_separate = Column(Boolean, default=False)
    bonus_amount = Column(Money, default=0)
    supervisor_dividend = Column(Money, default=0)
    remark = Column(Text, comment="複製自 SalaryRecord.remark")

    # ── 快照專屬 metadata ─────────────────────────────────────────────
    snapshot_type = Column(
        String(20),
        nullable=False,
        comment="month_end / finalize / manual",
    )
    captured_at = Column(
        DateTime, default=datetime.now, nullable=False, comment="快照捕捉時間"
    )
    captured_by = Column(String(50), comment="觸發者 username；系統自動觸發為 system")
    source_version = Column(Integer, comment="拍攝當下 SalaryRecord.version，便於追溯")
    snapshot_remark = Column(Text, comment="快照備註，手動類型常填")

    __table_args__ = (
        Index("ix_salary_snapshot_ym", "salary_year", "salary_month"),
        Index(
            "ix_salary_snapshot_emp_ym",
            "employee_id",
            "salary_year",
            "salary_month",
        ),
        Index(
            "ix_salary_snapshot_ym_type",
            "salary_year",
            "salary_month",
            "snapshot_type",
        ),
    )


class SalaryCalcJobRecord(Base):
    """薪資批次計算 async job 狀態表（DB-backed registry）。

    取代原 in-process dict 的 registry，讓多 worker 部署下：
    - find_active() 能跨 worker 看到同 year/month 的 active job，真正防止重複觸發
    - 任一 worker 查詢 /calculate-jobs/{id} 皆能讀到另一 worker 建立的 job 狀態
    """

    __tablename__ = "salary_calc_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(32), unique=True, nullable=False, comment="UUID hex")
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    status = Column(
        String(16),
        nullable=False,
        default="pending",
        comment="pending / running / completed / failed",
    )
    total = Column(Integer, nullable=False, default=0)
    done = Column(Integer, nullable=False, default=0)
    current_employee = Column(String(100), default="")
    results_json = Column(Text, nullable=True, comment="完成時 serialize 的結果列表")
    errors_json = Column(Text, nullable=True, comment="完成時 serialize 的錯誤列表")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_salary_calc_jobs_ym_status", "year", "month", "status"),
        Index("ix_salary_calc_jobs_job_id", "job_id"),
    )
