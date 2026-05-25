"""models/offboarding.py — 員工離職 checklist 模型"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base


class EmployeeOffboardingRecord(Base):
    """員工離職 checklist 紀錄（one-to-one with Employee）"""

    __tablename__ = "employee_offboarding_records"

    employee_id = Column(
        Integer,
        ForeignKey("employees.id", ondelete="CASCADE"),
        primary_key=True,
    )
    resign_date = Column(Date, nullable=False)
    resign_reason = Column(Text, nullable=True)

    opened_at = Column(DateTime, nullable=False)
    opened_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    user_revoked_at = Column(DateTime, nullable=True)
    appraisal_marked_at = Column(DateTime, nullable=True)
    leave_snapshot_at = Column(DateTime, nullable=True)
    certificate_generated_at = Column(DateTime, nullable=True)

    leave_balance_snapshot = Column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    certificate_pdf_path = Column(Text, nullable=True)
    nhi_unenroll_submitted_at = Column(DateTime, nullable=True)

    magic_link_token_hash = Column(Text, nullable=True)
    magic_link_expires_at = Column(DateTime, nullable=True)
    magic_link_revoked_at = Column(DateTime, nullable=True)
    magic_link_download_count = Column(Integer, default=0, nullable=False)
    magic_link_last_used_at = Column(DateTime, nullable=True)

    closed_at = Column(DateTime, nullable=True)
    closed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    employee = relationship(
        "Employee",
        back_populates="offboarding_record",
    )

    __table_args__ = (
        Index("ix_offboarding_resign_date", "resign_date"),
        Index(
            "ix_offboarding_open_status",
            "closed_at",
            postgresql_where=text("closed_at IS NULL"),
            sqlite_where=text("closed_at IS NULL"),
        ),
    )
