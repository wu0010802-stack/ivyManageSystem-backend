"""
models/config.py — 系統設定、考勤政策、獎金設定、保費率模型
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Date,
    DateTime,
    Boolean,
    ForeignKey,
    Text,
    UniqueConstraint,
    Index,
    JSON,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from models.base import Base
from models.types import Money


class AttendancePolicy(Base):
    """考勤政策表"""

    __tablename__ = "attendance_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_year = Column(Integer, nullable=False, default=0, comment="適用年度（西元）")
    version = Column(
        Integer, default=1, nullable=False, comment="版本號（每次更新遞增）"
    )
    changed_by = Column(String(50), nullable=True, comment="最後修改人")

    default_work_start = Column(String(5), default="08:00")
    default_work_end = Column(String(5), default="17:00")
    # Deprecated（不再進入薪資計算）：扣款固定以勞基法基準（月薪/30/8/60）
    # 計算，詳見 services/salary/deduction.py。欄位保留以維持資料庫相容性。
    late_deduction = Column(Float, default=50)
    early_leave_deduction = Column(Float, default=50)
    missing_punch_deduction = Column(Float, default=50)

    festival_bonus_months = Column(Integer, default=3)

    is_active = Column(Boolean, default=True)
    effective_date = Column(Date)

    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)


class BonusConfig(Base):
    """獎金設定表"""

    __tablename__ = "bonus_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_year = Column(Integer, nullable=False)
    version = Column(
        Integer, default=1, nullable=False, comment="版本號（每次更新遞增）"
    )
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

    principal_dividend = Column(Money, default=5000)
    director_dividend = Column(Money, default=4000)
    leader_dividend = Column(Money, default=3000)
    vice_leader_dividend = Column(Money, default=1500)

    overtime_head_normal = Column(Float, default=400)
    overtime_head_baby = Column(Float, default=450)
    overtime_assistant_normal = Column(Float, default=100)
    overtime_assistant_baby = Column(Float, default=150)

    school_wide_target = Column(Integer, default=160)

    # 園規常數（NULL = 沿用程式預設）
    meeting_default_hours = Column(
        Float,
        nullable=True,
        comment="每場園務會議計幾小時加班費（業主實務 2 hr）",
    )
    meeting_absence_penalty = Column(
        Integer,
        nullable=True,
        comment="缺席園務會議扣節慶獎金金額（預設 100 元）",
    )
    art_teacher_festival = Column(
        Float,
        nullable=True,
        comment="美語/才藝教師節慶獎金基數（A/B/C 同值，預設 2000）",
    )

    # 懲處扣款預設（DisciplinaryAction.deduction_amount=0 時 fallback 用）
    warning_deduction = Column(
        Float,
        nullable=False,
        default=1000,
        comment="警告一支預設扣節慶/超額獎金（業主慣例 1000）",
    )
    minor_offense_deduction = Column(
        Float,
        nullable=False,
        default=3000,
        comment="小過一支預設扣款（業主慣例 3000）",
    )
    major_offense_deduction = Column(
        Float,
        nullable=False,
        default=0,
        comment="大過一支預設扣款（業主未定，預設 0 由個案指定）",
    )

    # 年終 E化 Phase 2 規則欄位（2026-06-02 B1）
    # 才藝老師課時單價（float，NULL=未設定）
    art_teacher_unit_price = Column(
        Float,
        nullable=True,
        comment="才藝老師每課時單價（年終計算用，NULL=未設定）",
    )
    # 紅利門檻 — 舊生率
    dividend_returning_threshold = Column(
        Float,
        nullable=True,
        default=0.9,
        comment="紅利舊生率門檻（預設 0.9 = 90%）",
    )
    dividend_returning_amount = Column(
        Float,
        nullable=True,
        default=500,
        comment="達到舊生率門檻的紅利獎金金額（預設 500）",
    )
    # 紅利門檻 — 才藝參與率
    dividend_activity_threshold = Column(
        Float,
        nullable=True,
        default=0.8,
        comment="紅利才藝參與率門檻（預設 0.8 = 80%）",
    )
    dividend_activity_amount = Column(
        Float,
        nullable=True,
        default=1000,
        comment="達到才藝率門檻的紅利獎金金額（預設 1000）",
    )
    # 考勤費率
    late_deduction_per_time = Column(
        Float,
        nullable=True,
        default=50,
        comment="遲到每次扣年終款（年終定額罰則，預設 50 元；Excel 遲到一覽表）",
    )
    missing_punch_deduction_per_time = Column(
        Float,
        nullable=True,
        default=50,
        comment="未打卡每次扣年終款（年終定額罰則，預設 50 元；Excel 遲到一覽表）",
    )
    personal_leave_deduction_per_day = Column(
        Float,
        nullable=True,
        default=500,
        comment="事假每日扣年終款（預設 500 元）",
    )
    sick_leave_deduction_per_day = Column(
        Float,
        nullable=True,
        default=500,
        comment="病假每日扣年終款（預設 500 元）",
    )
    # 課後才藝班年終單價（JSON：班名或年齡組 → K 單價，NULL=未設定）
    after_class_award_unit_price = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
        comment="課後才藝班年終單價 JSON（班名→K 單價，NULL=未設定）",
    )
    # 才藝老師年終收款人（JSON：employee id list，每位得「全校總人次×art_teacher_unit_price」）
    # NULL/空 list = 未指定才藝老師 → 跳過才藝老師段（不報錯）
    art_teacher_employee_ids = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
        comment="才藝老師年終收款人 employee id list（JSON，NULL/空=未指定）",
    )

    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)


class GradeTarget(Base):
    """年級目標人數表"""

    __tablename__ = "grade_targets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_year = Column(Integer, nullable=False)
    grade_name = Column(String(20), nullable=False)
    bonus_config_id = Column(
        Integer,
        ForeignKey("bonus_configs.id"),
        nullable=True,
        comment="所屬獎金設定版本（NULL=舊資料）",
    )
    bonus_config = relationship("BonusConfig", backref="grade_targets")

    festival_two_teachers = Column(Integer, default=0)
    festival_one_teacher = Column(Integer, default=0)
    festival_shared = Column(Integer, default=0)

    overtime_two_teachers = Column(Integer, default=0)
    overtime_one_teacher = Column(Integer, default=0)
    overtime_shared = Column(Integer, default=0)

    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)


class InsuranceRate(Base):
    """勞健保費率表"""

    __tablename__ = "insurance_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rate_year = Column(Integer, nullable=False)
    version = Column(
        Integer, default=1, nullable=False, comment="版本號（每次更新遞增）"
    )
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

    # 三制度最高投保上限（NULL = 沿用程式預設常數，避免舊資料破功）
    labor_max_insured = Column(
        Integer, nullable=True, comment="勞保（含就保）最高月投保薪資"
    )
    health_max_insured = Column(Integer, nullable=True, comment="健保最高月投保金額")
    pension_max_insured = Column(Integer, nullable=True, comment="勞退最高月提繳工資")

    # 二代健保補充保費（兼職單筆給付 ≥ 門檻時扣）
    supplementary_health_rate = Column(
        Float,
        nullable=False,
        default=0.0211,
        comment="二代健保補充保費率（115 年 2.11%）",
    )
    supplementary_health_threshold = Column(
        Integer,
        nullable=False,
        default=29500,
        comment="兼職補充保費起扣門檻（現行基本工資 29500）",
    )

    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)


class InsuranceBracket(Base):
    """勞健保投保金額分級表（每年公告級距落地）

    取代原本 hardcode 在 services/insurance_service.py 的 INSURANCE_TABLE_2026。
    每年新公告級距時，行政只需新增 effective_year=新年度 的列即可，
    無需改程式 + 重新部署。歷史月份重算可依 effective_year 取對應級距。
    """

    __tablename__ = "insurance_brackets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    effective_year = Column(
        Integer,
        nullable=False,
        comment="適用年度（西元，與 InsuranceRate.rate_year 對齊）",
    )
    amount = Column(Integer, nullable=False, comment="投保金額")
    labor_employee = Column(Integer, nullable=False, comment="勞保員工自付")
    labor_employer = Column(Integer, nullable=False, comment="勞保雇主負擔")
    health_employee = Column(Integer, nullable=False, comment="健保員工自付（單口）")
    health_employer = Column(Integer, nullable=False, comment="健保雇主負擔")
    pension = Column(Integer, nullable=False, comment="勞退雇主提繳（6%）")

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, server_default=func.now(), onupdate=now_taipei_naive, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("effective_year", "amount", name="uq_bracket_year_amount"),
        Index("ix_bracket_year_amount", "effective_year", "amount"),
    )


class PositionSalaryConfig(Base):
    """職位標準底薪設定表"""

    __tablename__ = "position_salary_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_year = Column(Integer, nullable=False, default=0, comment="適用年度（西元）")
    head_teacher_a = Column(Money, default=39240, comment="A 級班導師標準底薪")
    head_teacher_b = Column(Money, default=37160, comment="B 級班導師標準底薪")
    head_teacher_c = Column(Money, default=33000, comment="C 級班導師標準底薪")
    assistant_teacher_a = Column(Money, default=35240, comment="A 級副班導師標準底薪")
    assistant_teacher_b = Column(Money, default=33000, comment="B 級副班導師標準底薪")
    assistant_teacher_c = Column(Money, default=29500, comment="C 級副班導師標準底薪")
    admin_staff = Column(Money, default=37160, comment="行政標準底薪")
    english_teacher = Column(Money, default=32500, comment="美語老師標準底薪")
    art_teacher = Column(Money, default=30000, comment="藝術老師標準底薪")
    designer = Column(Money, default=30000, comment="美編標準底薪")
    nurse = Column(Money, default=29800, comment="護理人員標準底薪")
    driver = Column(Money, default=30000, comment="司機標準底薪")
    kitchen_staff = Column(Money, default=29700, comment="廚房標準底薪")
    director = Column(Money, nullable=True, comment="主任標準底薪")
    principal = Column(Money, nullable=True, comment="園長標準底薪")
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

    created_at = Column(DateTime, default=now_taipei_naive)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)
