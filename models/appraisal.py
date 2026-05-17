"""教職員半年度考核（appraisal）SQLAlchemy ORM models。

對應半年考核 6 表 + 6 enum；欄位定義對齊
alembic/versions/20260511_a1p2p3r4i5s6_appraisal_init.py。

新版（M1 重構）對映 Excel 「114(上)年度考核統計表」16 項加減分結構：
- AppraisalScoreItem（每位 participant 16 筆加減分）取代過去過細的 AppraisalEvent
- AppraisalScoreItemCatalog（16 項定義表）取代 AppraisalPenaltyCatalogItem
- 等第 enum 仍用 OUTSTANDING/GOOD/PASS/WARN/FAIL（對應優/甲/乙/丙/丁）

FK 型別對齊原則：
- users/employees/classrooms（Integer）→ Integer
- appraisal_* / year_end_*（BigInteger）→ BigInteger
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON,
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
    """考核角色分群 — 對應獎金率分群 + 班級績效適用性。

    SUPERVISOR：園長、主任、組長等管理層
    HEAD_TEACHER：班導（含會計兼任班導視同班導群）
    ASSISTANT：副班導
    STAFF：辦公室行政（含會計獨立非班導）
    COOK：廚工/司機/儲備等支援角色（無班級績效）
    """

    SUPERVISOR = "SUPERVISOR"
    HEAD_TEACHER = "HEAD_TEACHER"
    ASSISTANT = "ASSISTANT"
    STAFF = "STAFF"
    COOK = "COOK"


class Grade(str, enum.Enum):
    """考核等第 — Excel 對應：優/甲/乙/丙/丁

    OUTSTANDING ≥ 90  優
    GOOD       80-89  甲
    PASS       70-79  乙（無獎金，連續 2 次或 2 年 3 次可調職降薪）
    WARN       60-69  丙（無獎金）
    FAIL       < 60   丁（得以解聘）
    """

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
    """16 項定義的加減分性質。

    POSITIVE：總是加分（如 才藝班參加率、特教生）
    NEGATIVE：總是扣分（如 請休假、遲到早退）
    NEUTRAL：可加可減（如 獎懲、3/15舊生註冊率、帶班人數）
    """

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"


class ScoreItemCode(str, enum.Enum):
    """14 條考核扣分項對應 Excel `半年考核統計表` 欄位。

    auto = aggregator 即時算；manual = 主任在 UI 上手填次數。
    """

    # auto (7) — engine 從 status_aggregator 拿原始值
    LATE_EARLY = "LATE_EARLY"
    MISSING_PUNCH = "MISSING_PUNCH"
    LEAVE = "LEAVE"
    RETURNING_RATE_0915 = "RETURNING_RATE_0915"
    RETURNING_RATE_0315 = "RETURNING_RATE_0315"
    AFTER_CLASS_RATE = "AFTER_CLASS_RATE"
    REWARD_PUNISH = "REWARD_PUNISH"
    # manual (7) — 主任在 ManualEventEntrySection 上填次數
    SCHOOL_MEETING_ABSENCE = "SCHOOL_MEETING_ABSENCE"
    INSTITUTION_MEETING_0913 = "INSTITUTION_MEETING_0913"
    INSTITUTION_MEETING_1115 = "INSTITUTION_MEETING_1115"
    SELF_IMPROVEMENT_ACTIVITY = "SELF_IMPROVEMENT_ACTIVITY"
    CHILD_ACCIDENT = "CHILD_ACCIDENT"
    CLASS_HEADCOUNT_BONUS = "CLASS_HEADCOUNT_BONUS"
    OTHER = "OTHER"


AUTO_ITEM_CODES = frozenset(
    {
        ScoreItemCode.LATE_EARLY,
        ScoreItemCode.MISSING_PUNCH,
        ScoreItemCode.LEAVE,
        ScoreItemCode.RETURNING_RATE_0915,
        ScoreItemCode.RETURNING_RATE_0315,
        ScoreItemCode.AFTER_CLASS_RATE,
        ScoreItemCode.REWARD_PUNISH,
    }
)
MANUAL_ITEM_CODES = frozenset(set(ScoreItemCode) - AUTO_ITEM_CODES)


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
    academic_year: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="民國學年"
    )
    semester: Mapped[Semester] = mapped_column(_SEMESTER_ENUM, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    base_score_calc_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="基礎分數計算基準日（如 9/15）"
    )
    base_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 2),
        nullable=False,
        default=Decimal("0"),
        comment="基礎分數 = 全園註冊人數 / 招生目標 × 100，所有 participant 共用",
    )
    enrollment_target: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="基準日的招生目標人數"
    )
    enrollment_actual: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, comment="基準日的實際註冊人數"
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
    hire_months_in_cycle: Mapped[Decimal] = mapped_column(
        Numeric(4, 1),
        nullable=False,
        default=Decimal("6"),
        comment="本週期內到職月數（半年最多 6）；用於到職未滿之比例底數",
    )
    is_excluded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="True=不計算考核（如未簽約、到職未滿一定期間）",
    )
    exclude_reason: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
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


class AppraisalScoreItemCatalog(Base):
    """16 項加減分項目的定義表。

    對應 Excel 半年考核表的 16 個欄位（1.請休假 / 2.遲到早退 / ... / 16.獎懲）。
    code 為跨資料庫穩定識別字串，display_order 控制 UI 與 Excel 欄位順序。
    """

    __tablename__ = "appraisal_score_item_catalog"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(60), nullable=False, comment="顯示名稱")
    sign: Mapped[ScoreItemSign] = mapped_column(_SCORE_ITEM_SIGN_ENUM, nullable=False)
    default_weight: Mapped[Decimal] = mapped_column(
        Numeric(4, 1), nullable=False, default=Decimal("0"), comment="每單位加減分權重"
    )
    data_source: Mapped[Optional[str]] = mapped_column(
        String(60),
        nullable=True,
        comment="資料來源（如 attendance / leave / manual / monthly_enrollment_snapshots）",
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AppraisalScoreItem(Base):
    """每位 participant 的 16 項加減分明細。

    取代舊版 AppraisalEvent；每筆對應 catalog 中的一個 item_code，
    一個 participant 最多 16 筆（catalog 有多少項目就最多幾筆）。
    獎懲（REWARD_PUNISH）可有多筆 — 同一 participant 多筆 REWARD_PUNISH。
    """

    __tablename__ = "appraisal_score_items"
    __table_args__ = (
        UniqueConstraint(
            "participant_id",
            "item_code",
            "sequence_no",
            name="uq_appraisal_score_item_unique",
        ),
    )

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
    catalog_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("appraisal_score_item_catalog.id", ondelete="SET NULL"),
        nullable=True,
    )
    item_code: Mapped[str] = mapped_column(
        String(40), nullable=False, comment="冗餘欄位，匯入時 catalog 可能未建即用"
    )
    sequence_no: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=1,
        comment="同一 item_code 多筆時的序號（獎懲可多筆，其他通常為 1）",
    )
    score_delta: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0"), comment="加減分數"
    )
    raw_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(8, 2),
        nullable=True,
        comment="原始資料（如休學人數、註冊率小數），方便回溯計算",
    )
    note: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="獎懲明細、特殊辦法說明"
    )
    source_ref: Mapped[Optional[str]] = mapped_column(
        String(60),
        nullable=True,
        comment="資料來源 ref（excel_row / attendance_id / disciplinary_id 等）",
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

    participant: Mapped[AppraisalParticipant] = relationship(
        back_populates="score_items"
    )
    catalog: Mapped[Optional[AppraisalScoreItemCatalog]] = relationship()


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
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )
    total_score: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )
    grade: Mapped[Grade] = mapped_column(
        _GRADE_ENUM, nullable=False, default=Grade.FAIL
    )
    bonus_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    leave_note: Mapped[Optional[str]] = mapped_column(
        String(120),
        nullable=True,
        comment="Excel 事假/病假備註欄（如「事3天」「病6天」）",
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
    """考核獎金率：(生效日, 角色群, 等第) → 底數。

    Excel 規則：園長/主任 8000、教師/行政會計、副班導/廚工 ... 各群基數不同；
    優等與甲等基數不同；獎金實額 = base_amount × (total_score / 100)。
    """

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


class AppraisalScoringRule(Base):
    """考核扣分規則版本化儲存。

    一個 item_code 可有多版（依 effective_from 區分）。
    rule_config JSON 結構依 rule_type 而異 — 詳見
    schemas/appraisal.py 的 PerUnitConfig / TierConfig /
    FlatThresholdConfig / DisciplinaryTieredConfig。
    """

    __tablename__ = "appraisal_scoring_rules"
    __table_args__ = (
        UniqueConstraint(
            "item_code",
            "effective_from",
            name="uq_appraisal_scoring_rule_code_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_code: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    rule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    applies_to_role_groups: Mapped[Optional[list]] = mapped_column(
        JSON,
        nullable=True,
        comment="null=全部；否則 ['HEAD_TEACHER','ASSISTANT_TEACHER',...]",
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AppraisalManualEventCount(Base):
    """主任在 UI 上手填的「事件型」item_code 次數。"""

    __tablename__ = "appraisal_manual_event_counts"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "participant_id",
            "item_code",
            name="uq_appraisal_manual_event_count_triple",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
        nullable=False,
    )
    participant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("appraisal_participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_code: Mapped[str] = mapped_column(String(64), nullable=False)
    count: Mapped[Decimal] = mapped_column(
        Numeric(8, 2),
        nullable=False,
        default=Decimal("0"),
        comment="次數；允許 0.5 半次",
    )
    entered_by: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class SummaryLogAction(str, enum.Enum):
    """考核 summary 簽核軌跡的動作型別。"""

    SIGN_SUPERVISOR = "SIGN_SUPERVISOR"
    SIGN_ACCOUNTING = "SIGN_ACCOUNTING"
    FINALIZE = "FINALIZE"
    REJECT = "REJECT"
    COMMENT = "COMMENT"
    RECOMPUTE = "RECOMPUTE"


_SUMMARY_LOG_ACTION_ENUM = Enum(
    SummaryLogAction,
    name="appraisal_summary_action",
    values_callable=lambda x: [e.value for e in x],
    create_type=False,
)


class AppraisalSummaryLog(Base):
    """考核 summary 簽核軌跡（誰簽的 / 何時 / 退簽原因 / 留言）。"""

    __tablename__ = "appraisal_summary_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    summary_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("appraisal_summaries.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[SummaryLogAction] = mapped_column(
        _SUMMARY_LOG_ACTION_ENUM, nullable=False
    )
    from_status: Mapped[Optional[SummaryStatus]] = mapped_column(
        _SUMMARY_STATUS_ENUM, nullable=True
    )
    to_status: Mapped[Optional[SummaryStatus]] = mapped_column(
        _SUMMARY_STATUS_ENUM, nullable=True
    )
    actor_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_role_snapshot: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
