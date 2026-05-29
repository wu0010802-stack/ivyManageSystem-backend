"""models/data_quality.py — Data quality invariant report."""

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)

from models.base import Base
from utils.taipei_time import now_taipei_naive


class DataQualityReport(Base):
    """每日 invariant 偵測結果，狀態：open/ack/fixed/ignored。"""

    __tablename__ = "data_quality_reports"
    __table_args__ = (
        Index("ix_dqr_rule_detected", "rule_code", "detected_at"),
        Index("ix_dqr_status_severity", "status", "severity"),
        Index(
            "ix_dqr_dedup_open",
            "dedup_key",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_code = Column(String(64), nullable=False)
    severity = Column(String(4), nullable=False)  # P0 / P1 / P2
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(String(50), nullable=False)
    summary = Column(Text, nullable=False)
    detected_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    last_seen_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    dedup_key = Column(String(64), nullable=False)
    status = Column(String(10), default="open", nullable=False)
    ack_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    ack_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    resolution_note = Column(Text, nullable=True)
