"""
models/config.py — 系統設定、考勤政策、獎金設定、保費率模型
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, Float, Date, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from models.base import Base


class AttendancePolicy(Base):
    """考勤政策表"""
    __tablename__ = "attendance_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(Integer, default=1, nullable=False, comment="版本號（每次更新遞增）")
    changed_by = Column(String(50), nullable=True, comment="最後修改人")

    default_work_start = Column(String(5), default="08:00")
    default_work_end = Column(String(5), default="17:00")
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
    version = Column(Integer, default=1, nullable=False, comment="版本號（每次更新遞增）")
    changed_by = Column(String(50), nullable=True, comment="最後修改人")

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
    bonus_config_id = Column(Integer, ForeignKey("bonus_configs.id"), nullable=True, comment="所屬獎金設定版本（NULL=舊資料）")
    bonus_config = relationship("BonusConfig", backref="grade_targets")

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
    version = Column(Integer, default=1, nullable=False, comment="版本號（每次更新遞增）")
    changed_by = Column(String(50), nullable=True, comment="最後修改人")

    labor_rate = Column(Float, default=0.125)
    labor_employee_ratio = Column(Float, default=0.20)
    labor_employer_ratio = Column(Float, default=0.70)
    labor_government_ratio = Column(Float, default=0.10)

    health_rate = Column(Float, default=0.0517)
    health_employee_ratio = Column(Float, default=0.30)
    health_employer_ratio = Column(Float, default=0.60)

    pension_employer_rate = Column(Float, default=0.06)

    average_dependents = Column(Float, default=0.56)

    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class PositionSalaryConfig(Base):
    """職位標準底薪設定表"""
    __tablename__ = "position_salary_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    head_teacher_a = Column(Float, default=39240, comment="A 級班導師標準底薪")
    head_teacher_b = Column(Float, default=37160, comment="B 級班導師標準底薪")
    head_teacher_c = Column(Float, default=33000, comment="C 級班導師標準底薪")
    assistant_teacher_a = Column(Float, default=35240, comment="A 級副班導師標準底薪")
    assistant_teacher_b = Column(Float, default=33000, comment="B 級副班導師標準底薪")
    assistant_teacher_c = Column(Float, default=29500, comment="C 級副班導師標準底薪")
    admin_staff = Column(Float, default=37160, comment="行政標準底薪")
    english_teacher = Column(Float, default=32500, comment="美語老師標準底薪")
    art_teacher = Column(Float, default=30000, comment="藝術老師標準底薪")
    designer = Column(Float, default=30000, comment="美編標準底薪")
    nurse = Column(Float, default=29800, comment="護理人員標準底薪")
    driver = Column(Float, default=30000, comment="司機標準底薪")
    kitchen_staff = Column(Float, default=29700, comment="廚房標準底薪")
    version = Column(Integer, default=1)
    changed_by = Column(String(50))
    created_at = Column(DateTime, server_default=func.now())


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
