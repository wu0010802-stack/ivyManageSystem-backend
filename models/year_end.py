"""年終獎金 SQLAlchemy ORM models。

對應 6 表 + 3 enum；欄位定義對齊 alembic init
(`20260515_b2c3d4e5f6a7_appraisal_yearend_init.py`)。

設計重點：
- `YearEndCycle`：每年一筆，params_snapshot 凍結計算當下的全校參數
- `YearEndEmployeeSnapshot`：仿 `SalarySnapshot` 的「不可變歷史」精神
- `SpecialBonusItem`：8 種特別獎金統一表，用 bonus_type 區分；per-type 差異塞 calc_meta JSONB
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
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.appraisal import RoleGroup as _RoleGroup
from models.appraisal import _ROLE_GROUP_ENUM  # type: ignore[attr-defined]
from models.base import Base


class YearEndCycleStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    CALCULATED = "CALCULATED"
    FINALIZED = "FINALIZED"
    PAID = "PAID"


class SpecialBonusType(str, enum.Enum):
    AFTER_CLASS_AWARD = "AFTER_CLASS_AWARD"
    EXCESS_ENROLLMENT = "EXCESS_ENROLLMENT"
    TEACHING_EXTRA = "TEACHING_EXTRA"
    SEMESTER_DIVIDEND_113_1 = "SEMESTER_DIVIDEND_113_1"
    SEMESTER_DIVIDEND_113_2 = "SEMESTER_DIVIDEND_113_2"
    FESTIVAL_DIFF = "FESTIVAL_DIFF"
    BIRTHDAY = "BIRTHDAY"
    OTHER = "OTHER"


class SettlementStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    CALCULATED = "CALCULATED"
    REVIEWED = "REVIEWED"
    FINALIZED = "FINALIZED"


_YEAR_END_STATUS_ENUM = Enum(
    YearEndCycleStatus,
    name="year_end_cycle_status_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)
_SPECIAL_BONUS_TYPE_ENUM = Enum(
    SpecialBonusType,
    name="year_end_special_bonus_type_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)
_SETTLEMENT_STATUS_ENUM = Enum(
    SettlementStatus,
    name="year_end_settlement_status_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)


class YearEndCycle(Base):
    __tablename__ = "year_end_cycles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    academic_year: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    status: Mapped[YearEndCycleStatus] = mapped_column(
        _YEAR_END_STATUS_ENUM,
        nullable=False,
        default=YearEndCycleStatus.DRAFT,
    )
    params_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
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

    org_settings: Mapped[Optional["YearEndOrgSettings"]] = relationship(
        back_populates="cycle", uselist=False, cascade="all, delete-orphan"
    )
    class_targets: Mapped[list["YearEndClassTarget"]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )
    employee_snapshots: Mapped[list["YearEndEmployeeSnapshot"]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )
    settlements: Mapped[list["YearEndSettlement"]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )


class YearEndOrgSettings(Base):
    __tablename__ = "year_end_org_settings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    total_enrollment_target: Mapped[int] = mapped_column(Integer, nullable=False)
    achievement_rate_first: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False
    )
    achievement_rate_second: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False
    )
    org_achievement_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False
    )
    festival_bonus_total_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    org_meeting_deduction: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    extras_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    cycle: Mapped[YearEndCycle] = relationship(back_populates="org_settings")


class YearEndClassTarget(Base):
    __tablename__ = "year_end_class_targets"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id", "classroom_id", name="uq_year_end_class_target"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    classroom_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("classrooms.id", ondelete="RESTRICT"), nullable=False
    )
    staffing_target: Mapped[int] = mapped_column(Integer, nullable=False)
    achievement_rate_first: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False
    )
    achievement_rate_second: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False
    )
    returning_rate_first: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    returning_rate_second: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )

    cycle: Mapped[YearEndCycle] = relationship(back_populates="class_targets")


class YearEndEmployeeSnapshot(Base):
    __tablename__ = "year_end_employee_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id", "employee_id", name="uq_year_end_employee_snapshot"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id: Mapped[int] = mapped_column(Integer, nullable=False)
    base_salary: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    festival_total: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    role_group: Mapped[_RoleGroup] = mapped_column(
        _ROLE_GROUP_ENUM, nullable=False
    )
    hire_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    classroom_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("classrooms.id", ondelete="SET NULL"), nullable=True
    )
    is_resigned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resign_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    is_contracted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cycle: Mapped[YearEndCycle] = relationship(back_populates="employee_snapshots")
    settlement: Mapped[Optional["YearEndSettlement"]] = relationship(
        back_populates="snapshot", uselist=False
    )


class YearEndSettlement(Base):
    __tablename__ = "year_end_settlements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("year_end_employee_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    employee_id: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_performance_rate: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )
    gross_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    subtotal_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    deduction_total: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    deduction_late: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    deduction_personal_leave: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    deduction_sick_leave: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    deduction_meeting: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    deduction_disciplinary: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    deduction_parental_leave: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    payable_subtotal: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    special_bonus_sum: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    calc_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    status: Mapped[SettlementStatus] = mapped_column(
        _SETTLEMENT_STATUS_ENUM,
        nullable=False,
        default=SettlementStatus.DRAFT,
    )
    calculated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finalized_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finalized_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cycle: Mapped[YearEndCycle] = relationship(back_populates="settlements")
    snapshot: Mapped[YearEndEmployeeSnapshot] = relationship(
        back_populates="settlement"
    )


class YearEndSpecialBonusItem(Base):
    __tablename__ = "year_end_special_bonus_items"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "employee_id",
            "bonus_type",
            "period_label",
            name="uq_year_end_special_bonus",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(
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
        String(20), nullable=False, default=""
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    calc_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
