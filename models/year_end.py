"""年終獎金（year-end bonus）SQLAlchemy ORM models。

對應 Excel「114年年終經營績效」22 sheets 的核心結算流程：6 表 + 3 enum。

6 表設計：
1. year_end_cycles               每年一筆週期
2. org_year_settings             全校年度設定（招生目標、達成比率、節慶獎金額度）
3. class_enrollment_targets      班級每學期編制人數與目標達成率
4. employee_year_end_snapshot    每 cycle 每員工 snapshot（基本薪俸/節慶/到職資訊）
5. year_end_settlements          每人一筆結算單（6 層計算結果）
6. special_bonus_items           統一表：8 種特別獎金（學期紅利/鼓勵才藝/教課/超額/節慶差額...）

6 層年終計算（settlement 欄位順序）：
  step1 平均績效 = avg(全校達成率上下、班級舊生達成率上下、班級經營績效上下)
  step2 年終毛額 = (base_salary + festival_total) × 平均績效%
  step3 小計 = 毛額 × 機構達成比率
  step4 扣項 = 請假遲到 + 自強活動/機構會議 + 事假 + 病假 + 遲到早退 + 獎懲
  step5 應領小計 = (小計 + 扣項) × 到職月數 / 12
  step6 年終總額 = 應領小計 + Σ special_bonus_items

FK 型別對齊：employees/classrooms/users → Integer；year_end_* / appraisal_* → BigInteger。
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class YearEndCycleStatus(str, enum.Enum):
    OPEN = "OPEN"
    LOCKED = "LOCKED"
    CLOSED = "CLOSED"


class YearEndSettlementStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SUPERVISOR_SIGNED = "SUPERVISOR_SIGNED"
    ACCOUNTING_SIGNED = "ACCOUNTING_SIGNED"
    FINALIZED = "FINALIZED"


class SpecialBonusType(str, enum.Enum):
    """9 種特別獎金 + 1 通用類型。

    對應 Excel「年終獎金總表」B 欄各列：
      APPRAISAL_HALF_BONUS_FIRST  : 較早那一筆（前一完整學年上學期）— 來自 appraisal_summaries
      APPRAISAL_HALF_BONUS_SECOND : 較晚那一筆（前一完整學年下學期）— 來自 appraisal_summaries
      SEMESTER_DIVIDEND_FIRST     : N上學期紅利（舊生 500 + 才藝 1000）
      SEMESTER_DIVIDEND_SECOND    : N下學期紅利
      AFTER_CLASS_AWARD           : N上鼓勵推動才藝班獎金（按班級人數）
      TEACHING_EXTRA              : N上教課教師獎勵金（堂數 × 65/堂）
      EXCESS_ENROLLMENT           : N上超額獎金（每月超額幼生）
      FESTIVAL_DIFF               : N.8-N+1.01 節慶獎金差額（多退少補，可為負）
      CUSTOM                      : 其他客製化（保留擴充用）

    FIRST=較早=前一完整學年上學期（Semester.FIRST=上），SECOND=較晚=前一完整學年下學期（Semester.SECOND=下）；
    兩者方向一致（無反轉）。由 services/year_end/appraisal_sync.py 依 calendar payout year 自動 map。
    """

    APPRAISAL_HALF_BONUS_FIRST = "APPRAISAL_HALF_BONUS_FIRST"
    APPRAISAL_HALF_BONUS_SECOND = "APPRAISAL_HALF_BONUS_SECOND"
    SEMESTER_DIVIDEND_FIRST = "SEMESTER_DIVIDEND_FIRST"
    SEMESTER_DIVIDEND_SECOND = "SEMESTER_DIVIDEND_SECOND"
    AFTER_CLASS_AWARD = "AFTER_CLASS_AWARD"
    TEACHING_EXTRA = "TEACHING_EXTRA"
    EXCESS_ENROLLMENT = "EXCESS_ENROLLMENT"
    FESTIVAL_DIFF = "FESTIVAL_DIFF"
    CUSTOM = "CUSTOM"


_YEAR_END_CYCLE_STATUS_ENUM = Enum(
    YearEndCycleStatus,
    name="year_end_cycle_status_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)
_YEAR_END_SETTLEMENT_STATUS_ENUM = Enum(
    YearEndSettlementStatus,
    name="year_end_settlement_status_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)
_SPECIAL_BONUS_TYPE_ENUM = Enum(
    SpecialBonusType,
    name="year_end_special_bonus_type_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)


class YearEndCycle(Base):
    """年度週期，一個民國學年一筆（學年 = N年8月～N+1年7月）。"""

    __tablename__ = "year_end_cycles"
    __table_args__ = (UniqueConstraint("academic_year", name="uq_year_end_cycle_year"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    academic_year: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="民國學年"
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    bonus_calc_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="結算基準日（如 1/15）"
    )
    status: Mapped[YearEndCycleStatus] = mapped_column(
        _YEAR_END_CYCLE_STATUS_ENUM, nullable=False, default=YearEndCycleStatus.OPEN
    )
    params_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="鎖定當下的計算參數 snapshot（如機構達成比率、扣款規則版本）",
    )
    created_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    settlements: Mapped[list["YearEndSettlement"]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )


class OrgYearSettings(Base):
    """全校年度設定 — 招生目標、機構達成比率、節慶獎金額度等。

    Excel 對應：「114學年度第01學期幼生每月人數統計表」「年終獎金」sheet 的「達成比率」。
    每學期一筆（半年），同 cycle 有兩筆（FIRST/SECOND semester）。
    """

    __tablename__ = "org_year_settings"
    __table_args__ = (
        UniqueConstraint(
            "year_end_cycle_id", "semester_first", name="uq_org_year_settings_sem"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    year_end_cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    semester_first: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="True=上學期(8-1月) / False=下學期(2-7月)",
    )
    enrollment_target: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="招生目標人數（例 160）"
    )
    enrollment_actual: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="基準日實際註冊人數（例 121）"
    )
    school_achievement_rate: Mapped[Decimal] = mapped_column(
        Numeric(6, 3),
        nullable=False,
        default=Decimal("0"),
        comment="全校目標達成率 = actual/target × 100",
    )
    org_achievement_rate: Mapped[Decimal] = mapped_column(
        Numeric(6, 3),
        nullable=False,
        default=Decimal("0"),
        comment="機構達成比率（年終獎金 step3 用，例 83.6 或 91.5）",
    )
    meeting_absence_deduction: Mapped[Decimal] = mapped_column(
        Numeric(8, 2),
        nullable=False,
        default=Decimal("1000"),
        comment="自強活動/機構會議單次未參加扣款（預設 1000）",
    )
    festival_bonus_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="節慶獎金額度設定 snapshot；通常從 BonusConfig 帶入",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ClassEnrollmentTarget(Base):
    """班級每學期編制人數 + 舊生註冊率目標。

    Excel 對應：「班級經營績效 114.01.15」的「編制人數」「舊生註冊率」欄。
    用於年終獎金 step1 計算「班級經營績效」與「班級舊生達成率」。
    """

    __tablename__ = "class_enrollment_targets"
    __table_args__ = (
        UniqueConstraint(
            "year_end_cycle_id",
            "semester_first",
            "classroom_id",
            name="uq_class_enrollment_target",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    year_end_cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    semester_first: Mapped[bool] = mapped_column(Boolean, nullable=False)
    classroom_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("classrooms.id", ondelete="CASCADE"), nullable=False
    )
    head_teacher_employee_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    assistant_employee_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    head_count_target: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="編制人數"
    )
    avg_monthly_enrollment: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0"), comment="6 月平均在籍"
    )
    class_performance_rate: Mapped[Decimal] = mapped_column(
        Numeric(6, 2),
        nullable=False,
        default=Decimal("0"),
        comment="班級經營績效 = 平均在籍 / 編制 × 100",
    )
    returning_student_rate: Mapped[Decimal] = mapped_column(
        Numeric(6, 3),
        nullable=False,
        default=Decimal("0"),
        comment="班級舊生註冊率（小數，如 0.926）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EmployeeYearEndSnapshot(Base):
    """每 cycle 每員工的年終結算 snapshot。

    比照 SalaryRecord 的稽核 snapshot 慣例：把計算當下的員工屬性凍結，
    避免員工資料事後變動影響歷史結算。employees 表本身不加新欄位。
    """

    __tablename__ = "employee_year_end_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "year_end_cycle_id",
            "employee_id",
            name="uq_employee_year_end_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    year_end_cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    base_salary: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0"),
        comment="snapshot 當下基本薪俸",
    )
    festival_total: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0"),
        comment="年度節慶獎金總額（2/6/9/12 月加總，從 services/salary/festival 取得）",
    )
    role: Mapped[Optional[str]] = mapped_column(
        String(40), nullable=True, comment="snapshot 當下職稱/職務"
    )
    classroom_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("classrooms.id", ondelete="SET NULL"), nullable=True
    )
    hire_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    resign_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    hire_months: Mapped[Decimal] = mapped_column(
        Numeric(4, 1),
        nullable=False,
        default=Decimal("12"),
        comment="本 cycle 到職月數，最大 12（year_end step5 比例底數）",
    )
    is_resigned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="cycle 期間是否離職"
    )
    is_contracted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="是否已簽約；False=未簽約（不計入年終）",
    )
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="額外欄位兜底（職務變動、班導兼任等）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class YearEndSettlement(Base):
    """每員工一筆年終結算單，含 6 層計算結果與簽核流程。

    對應 Excel「年終獎金」「年終獎金總表」每行員工。
    """

    __tablename__ = "year_end_settlements"
    __table_args__ = (
        UniqueConstraint(
            "year_end_cycle_id",
            "employee_id",
            name="uq_year_end_settlement_cycle_emp",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    year_end_cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("employee_year_end_snapshot.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # step 1: 平均績效
    school_rate_first: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(6, 2), nullable=True, comment="全校達成率上學期"
    )
    school_rate_second: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    class_returning_rate_first: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    class_returning_rate_second: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    class_performance_rate_first: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    class_performance_rate_second: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    avg_performance_rate: Mapped[Decimal] = mapped_column(
        Numeric(6, 2),
        nullable=False,
        default=Decimal("0"),
        comment="step1 平均績效 %",
    )

    # step 2: 毛額
    base_salary: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    festival_total: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    gross_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0"),
        comment="step2 毛額 = (base_salary + festival_total) × avg_performance_rate%",
    )

    # step 3: 小計
    org_achievement_rate: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), nullable=False, default=Decimal("0")
    )
    subtotal_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0"),
        comment="step3 小計 = gross_amount × org_achievement_rate",
    )

    # step 4: 扣項（皆為負或 0）
    deduction_leave_late: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0"),
        comment="去年底前請假遲到合併",
    )
    deduction_meeting: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0"),
        comment="自強活動/機構會議未參加",
    )
    deduction_personal_leave: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0"), comment="事假"
    )
    deduction_sick_leave: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0"), comment="病假/育嬰假"
    )
    deduction_late: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0"), comment="遲到早退"
    )
    deduction_disciplinary: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0"),
        comment="獎懲（大過 -6000 等）",
    )
    deduction_total: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0"), comment="扣項總計（為負）"
    )

    # step 5: 應領小計
    hire_months: Mapped[Decimal] = mapped_column(
        Numeric(4, 1), nullable=False, default=Decimal("12")
    )
    proration_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4),
        nullable=False,
        default=Decimal("1"),
        comment="到職比例 = hire_months/12",
    )
    payable_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0"),
        comment="step5 = (subtotal + deduction_total) × proration_rate",
    )

    # step 6: 年終總額
    special_bonus_total: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0"),
        comment="SUM(special_bonus_items.amount) for this employee+cycle",
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0"),
        comment="step6 = payable_amount + special_bonus_total",
    )

    calc_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="計算過程稽核 meta（原始 Excel cell 值、各步中間值、扣項明細）",
    )
    remark: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="Excel 備註欄（到職日期、簽約日期等）"
    )

    status: Mapped[YearEndSettlementStatus] = mapped_column(
        _YEAR_END_SETTLEMENT_STATUS_ENUM,
        nullable=False,
        default=YearEndSettlementStatus.DRAFT,
    )
    supervisor_signed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    supervisor_signed_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    accounting_signed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accounting_signed_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    finalized_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finalized_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rejected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejected_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rejected_from_stage: Mapped[Optional[YearEndSettlementStatus]] = mapped_column(
        _YEAR_END_SETTLEMENT_STATUS_ENUM, nullable=True
    )
    rejected_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cycle: Mapped[YearEndCycle] = relationship(back_populates="settlements")


class SpecialBonusItem(Base):
    """8 種特別獎金統一表。

    Excel 對應「年終獎金總表」B 欄 8 行特別獎金；採統一表降低 query 複雜度：
    - 加總：SELECT SUM(amount) GROUP BY employee_id, year_end_cycle_id
    - 列印條：單一查詢即可組出所有特別獎金行
    - per-type 結構差異存 calc_meta JSONB（class_id、參加率、課堂數、單價...）
    """

    __tablename__ = "special_bonus_items"
    __table_args__ = (
        UniqueConstraint(
            "year_end_cycle_id",
            "employee_id",
            "bonus_type",
            "period_label",
            name="uq_special_bonus_item",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    year_end_cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    bonus_type: Mapped[SpecialBonusType] = mapped_column(
        _SPECIAL_BONUS_TYPE_ENUM, nullable=False
    )
    period_label: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default="",
        comment="期間標籤（如 113上、114-08、114上）；upsert 鍵之一",
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        default=Decimal("0"),
        comment="獎金金額；FESTIVAL_DIFF 可為負（多退）",
    )
    classroom_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("classrooms.id", ondelete="SET NULL"), nullable=True
    )
    calc_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="per-type 計算明細：{lessons, rate, excess_count, participation_rate...}",
    )
    source_ref: Mapped[Optional[str]] = mapped_column(
        String(120),
        nullable=True,
        comment="來源 Excel sheet 名或內部 ref（appraisal_summary_id 等）",
    )
    created_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
