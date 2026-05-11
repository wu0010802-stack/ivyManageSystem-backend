"""教職員考核（appraisal）SQLAlchemy ORM models。

對應 5 表 + 1 catalog 表 + 8 enum；欄位定義對齊
alembic/versions/20260511_v7w8x9y0z1a2_appraisal_init.py。

注意：FK columns 對齊 migration 的 Integer/BigInteger 區分：
- 引用 users/employees/classrooms (Integer) → Integer
- 引用 appraisal_* (BigInteger) → BigInteger
- 表自己的 id → BigInteger
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


class EventType(str, enum.Enum):
    MAJOR_MERIT = "MAJOR_MERIT"
    MINOR_MERIT = "MINOR_MERIT"
    COMMENDATION = "COMMENDATION"
    WARNING = "WARNING"
    MINOR_DEMERIT = "MINOR_DEMERIT"
    MAJOR_DEMERIT = "MAJOR_DEMERIT"
    ORAL_WARNING = "ORAL_WARNING"
    SCORE_ADJUST = "SCORE_ADJUST"


class ParentReaction(str, enum.Enum):
    NONE = "none"
    FORGIVEN = "forgiven"
    WITHDRAWAL = "withdrawal"
    LITIGATION = "litigation"
    COMPLAINT = "complaint"
    MEDIA = "media"


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


class CatalogCategory(str, enum.Enum):
    MISCONDUCT = "MISCONDUCT"
    MEDICATION = "MEDICATION"
    ACCIDENT = "ACCIDENT"
    DISPUTE = "DISPUTE"
    NEGLIGENCE = "NEGLIGENCE"
    MERIT = "MERIT"
    SPECIAL = "SPECIAL"


# 共用 enum types（對齊 migration 的 PG enum 名稱；create_type=False 因 enum 由 migration 創建）
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
_EVENT_TYPE_ENUM = Enum(
    EventType,
    name="appraisal_event_type_enum",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)
_PARENT_REACTION_ENUM = Enum(
    ParentReaction,
    name="appraisal_parent_reaction_enum",
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
_CATALOG_CATEGORY_ENUM = Enum(
    CatalogCategory,
    name="appraisal_catalog_category_enum",
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cycle: Mapped[AppraisalCycle] = relationship(back_populates="participants")
    events: Mapped[list["AppraisalEvent"]] = relationship(
        back_populates="participant", cascade="all, delete-orphan"
    )
    summary: Mapped[Optional["AppraisalSummary"]] = relationship(
        back_populates="participant", uselist=False, cascade="all, delete-orphan"
    )


class AppraisalPenaltyCatalogItem(Base):
    __tablename__ = "appraisal_penalty_catalog"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    category: Mapped[CatalogCategory] = mapped_column(
        _CATALOG_CATEGORY_ENUM, nullable=False
    )
    subcategory: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    default_event_type: Mapped[EventType] = mapped_column(
        _EVENT_TYPE_ENUM, nullable=False
    )
    default_score_delta: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    severity_max: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AppraisalEvent(Base):
    __tablename__ = "appraisal_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    participant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("appraisal_participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    catalog_item_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("appraisal_penalty_catalog.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[EventType] = mapped_column(_EVENT_TYPE_ENUM, nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    score_delta: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    severity_level: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    parent_reaction: Mapped[Optional[ParentReaction]] = mapped_column(
        _PARENT_REACTION_ENUM, nullable=True
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    created_by: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reverted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reverted_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reverted_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    participant: Mapped[AppraisalParticipant] = relationship(back_populates="events")
    catalog_item: Mapped[Optional[AppraisalPenaltyCatalogItem]] = relationship()


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
    event_score_sum: Mapped[Decimal] = mapped_column(
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
