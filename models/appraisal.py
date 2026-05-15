"""半年考核（appraisal）SQLAlchemy ORM models。

對應 6 表 + 6 enum；欄位定義對齊 alembic init migration
(`20260515_b2c3d4e5f6a7_appraisal_yearend_init.py`)。

FK 慣例：引用 users / employees / classrooms 為 Integer；其餘 appraisal_* 為 BigInteger。
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from models.base import Base


class Semester(str, enum.Enum):
    FIRST = "FIRST"
    SECOND = "SECOND"


class CycleStatus(str, enum.Enum):
    OPEN = "OPEN"
    LOCKED = "LOCKED"
    CLOSED = "CLOSED"


class RoleGroup(str, enum.Enum):
    SUPERVISOR = "SUPERVISOR"
    HEAD_TEACHER = "HEAD_TEACHER"
    ASSISTANT = "ASSISTANT"
    STAFF = "STAFF"
    COOK = "COOK"


class Grade(str, enum.Enum):
    OUTSTANDING = "OUTSTANDING"
    GOOD = "GOOD"
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class SummaryStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    SUPERVISOR_SIGNED = "SUPERVISOR_SIGNED"
    ACCOUNTING_SIGNED = "ACCOUNTING_SIGNED"
    FINALIZED = "FINALIZED"


class ScoreItemSign(str, enum.Enum):
    PLUS = "PLUS"
    MINUS = "MINUS"
    BOTH = "BOTH"


_SEMESTER_ENUM = Enum(
    Semester,
    name="appraisal_semester_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)
_CYCLE_STATUS_ENUM = Enum(
    CycleStatus,
    name="appraisal_cycle_status_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)
_ROLE_GROUP_ENUM = Enum(
    RoleGroup,
    name="appraisal_role_group_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)
_GRADE_ENUM = Enum(
    Grade,
    name="appraisal_grade_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)
_SUMMARY_STATUS_ENUM = Enum(
    SummaryStatus,
    name="appraisal_summary_status_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)
_SCORE_ITEM_SIGN_ENUM = Enum(
    ScoreItemSign,
    name="appraisal_score_item_sign_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)


class AppraisalCycle(Base):
    __tablename__ = "appraisal_cycles"
    __table_args__ = (
        UniqueConstraint(
            "academic_year", "semester", name="uq_appraisal_cycle_year_sem"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    academic_year: Mapped[int] = mapped_column(Integer, nullable=False)
    semester: Mapped[Semester] = mapped_column(_SEMESTER_ENUM, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    base_score_calc_date: Mapped[date] = mapped_column(Date, nullable=False)
    base_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    status: Mapped[CycleStatus] = mapped_column(
        _CYCLE_STATUS_ENUM, nullable=False, default=CycleStatus.OPEN
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

    participants: Mapped[list["AppraisalParticipant"]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan"
    )


class AppraisalScoreItemCatalog(Base):
    __tablename__ = "appraisal_score_item_catalog"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    sign: Mapped[ScoreItemSign] = mapped_column(_SCORE_ITEM_SIGN_ENUM, nullable=False)
    default_weight: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("1")
    )
    data_source: Mapped[str] = mapped_column(
        String(40), nullable=False, default="manual"
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AppraisalParticipant(Base):
    __tablename__ = "appraisal_participants"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id", "employee_id", name="uq_appraisal_participant_cycle_emp"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    role_group: Mapped[RoleGroup] = mapped_column(_ROLE_GROUP_ENUM, nullable=False)
    classroom_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("classrooms.id", ondelete="SET NULL"), nullable=True
    )
    base_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    target_enrollment: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    actual_enrollment: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hire_months_in_cycle: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("6")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cycle: Mapped[AppraisalCycle] = relationship(back_populates="participants")
    score_items: Mapped[list["AppraisalScoreItem"]] = relationship(
        back_populates="participant", cascade="all, delete-orphan"
    )
    summary: Mapped[Optional["AppraisalSummary"]] = relationship(
        back_populates="participant", uselist=False, cascade="all, delete-orphan"
    )


class AppraisalScoreItem(Base):
    __tablename__ = "appraisal_score_items"
    __table_args__ = (
        UniqueConstraint(
            "participant_id", "item_code", name="uq_appraisal_score_item"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    participant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("appraisal_participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_code: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("appraisal_score_item_catalog.code", ondelete="RESTRICT"),
        nullable=False,
    )
    score_delta: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    raw_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    participant: Mapped[AppraisalParticipant] = relationship(
        back_populates="score_items"
    )
    catalog_item: Mapped[Optional[AppraisalScoreItemCatalog]] = relationship(
        primaryjoin=(
            "AppraisalScoreItem.item_code == AppraisalScoreItemCatalog.code"
        ),
        foreign_keys=[item_code],
        viewonly=True,
    )


class AppraisalSummary(Base):
    __tablename__ = "appraisal_summaries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    participant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("appraisal_participants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    base_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    item_score_sum: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    total_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    grade: Mapped[Grade] = mapped_column(
        _GRADE_ENUM, nullable=False, default=Grade.FAIL
    )
    bonus_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    leave_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[SummaryStatus] = mapped_column(
        _SUMMARY_STATUS_ENUM, nullable=False, default=SummaryStatus.DRAFT
    )
    supervisor_signed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    supervisor_signed_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    supervisor_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    accounting_signed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accounting_signed_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    accounting_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    finalized_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finalized_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    finalized_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rejected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejected_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rejected_from_stage: Mapped[Optional[SummaryStatus]] = mapped_column(
        _SUMMARY_STATUS_ENUM, nullable=True
    )
    rejected_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    participant: Mapped[AppraisalParticipant] = relationship(back_populates="summary")


class AppraisalBonusRate(Base):
    __tablename__ = "appraisal_bonus_rates"
    __table_args__ = (
        UniqueConstraint(
            "effective_from", "role_group", "grade", name="uq_appraisal_bonus_rate"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    role_group: Mapped[RoleGroup] = mapped_column(_ROLE_GROUP_ENUM, nullable=False)
    grade: Mapped[Grade] = mapped_column(_GRADE_ENUM, nullable=False)
    base_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    created_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
