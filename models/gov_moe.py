"""MOE reporting module models (Phase 1: 4 tables)."""

from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    Numeric,
    String,
    Text,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    JSON,  # JSONB upgrade pending: blocked by SQLite test infra
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import relationship

from models.base import Base
from models.types import Money


class StudentDisabilityDocument(Base):
    """身障/特教相關文件附件（Phase 1 主用）"""

    __tablename__ = "student_disability_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(
        Integer,
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    doc_type = Column(
        String(20),
        nullable=False,
        comment="鑑定證明/身障手冊/IEP/評估報告/其他",
    )
    file_path = Column(
        String(500), nullable=False, comment="檔案路徑（既有 attachments 整合）"
    )
    issued_date = Column(Date, nullable=True, comment="開立/取得日期")
    expiry_date = Column(Date, nullable=True, comment="到期日（無則永久）")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )

    __table_args__ = (
        Index("ix_disability_docs_student_type", "student_id", "doc_type"),
    )


class StudentIEPRecord(Base):
    """IEP 個別化教育計畫（Phase 4 內容；Phase 1 建空殼）"""

    __tablename__ = "student_iep_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(
        Integer,
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    school_year = Column(Integer, nullable=False, comment="學年（如 2025）")
    semester = Column(Integer, nullable=False, comment="學期 1/2")
    status = Column(
        String(20),
        default="draft",
        server_default="draft",
        nullable=False,
        comment="draft/pending_review/approved/closed",
    )
    current_status = Column(Text, nullable=True, comment="目前發展狀況評估")
    long_term_goals = Column(Text, nullable=True, comment="長期目標")
    short_term_goals = Column(JSON, nullable=True, comment="短期目標 list")
    mid_term_evaluation = Column(Text, nullable=True)
    final_evaluation = Column(Text, nullable=True)
    iep_team_members = Column(JSON, nullable=True)
    meeting_dates = Column(JSON, nullable=True)
    created_by_employee_id = Column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    approved_by_employee_id = Column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "student_id", "school_year", "semester", name="uq_iep_student_year_semester"
        ),
    )


class SpecialEducationSubsidy(Base):
    """特教加給/助理鐘點費申領（Phase 4 內容；Phase 1 建空殼）"""

    __tablename__ = "special_education_subsidies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subsidy_type = Column(
        String(30), nullable=False, comment="teacher_extra / assistant_hourly"
    )
    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    related_student_ids = Column(JSON, nullable=True, comment="服務的身障幼生 id list")
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    hours_or_rate = Column(
        Numeric(8, 2), nullable=True, comment="時數（鐘點，可帶 .5）"
    )
    amount_requested = Column(Money, nullable=False, default=0, server_default="0")
    amount_approved = Column(Money, nullable=True)
    status = Column(
        String(20),
        default="draft",
        server_default="draft",
        nullable=False,
        comment="draft/submitted/approved/paid/rejected",
    )
    applied_at = Column(DateTime, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    paid_at = Column(DateTime, nullable=True)
    approval_doc_path = Column(String(500), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, nullable=False
    )


class MonthlyEnrollmentSnapshot(Base):
    """月報快照（Phase 2 內容；Phase 1 建空殼）"""

    __tablename__ = "monthly_enrollment_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    classroom_id = Column(
        Integer,
        ForeignKey("classrooms.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    age_group = Column(String(10), nullable=True, comment="2-3 / 3-4 / 4-5 / 5-6")
    total_count = Column(Integer, nullable=False, default=0, server_default="0")
    male_count = Column(Integer, nullable=False, default=0, server_default="0")
    female_count = Column(Integer, nullable=False, default=0, server_default="0")
    disadvantaged_count = Column(Integer, nullable=False, default=0, server_default="0")
    disability_count = Column(Integer, nullable=False, default=0, server_default="0")
    indigenous_count = Column(Integer, nullable=False, default=0, server_default="0")
    foreign_count = Column(Integer, nullable=False, default=0, server_default="0")
    expected_attendance_days = Column(
        Integer, nullable=False, default=0, server_default="0"
    )
    actual_attendance_days = Column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attendance_rate = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="百分比×100 整數",
    )
    snapshot_date = Column(Date, nullable=True)
    generated_at = Column(DateTime, nullable=True)
    generated_by = Column(String(100), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "year",
            "month",
            "classroom_id",
            "age_group",
            name="uq_monthly_snapshot_key",
        ),
    )
