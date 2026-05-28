"""models/medical_access_log.py — 兒童醫療欄位取用稽核。

P0d-2 法規/個資：個資法 §6 特種個資取用獨立稽核（不與 audit_log 混）。

Refs: docs/superpowers/specs/2026-05-28-medical-fields-encryption-design.md §3.4
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)

from models.base import Base
from utils.taipei_time import now_taipei_naive

# Field name 列舉（給 endpoint 寫 log 時用）
MEDICAL_FIELD_ALLERGY = "allergy"
MEDICAL_FIELD_MEDICATION = "medication"
MEDICAL_FIELD_SPECIAL_NEEDS = "special_needs"
MEDICAL_FIELD_TEMPERATURE = "temperature_c"
MEDICAL_FIELD_BUNDLE = "bundle"  # 一次讀取全部醫療欄位


class MedicalAccessLog(Base):
    """每筆 = 一次醫療欄位讀取記錄。"""

    __tablename__ = "medical_access_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="取用者 user_id（離職員工 deleted 後可變 NULL 保留稽核軌跡）",
    )
    student_id = Column(
        Integer,
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    field_name = Column(
        String(50),
        nullable=False,
        comment="allergy / medication / special_needs / temperature_c / bundle",
    )
    reason = Column(Text, nullable=False, comment="取用理由（endpoint 層 ≥10 字 gate）")
    accessed_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    ip_address = Column(String(45), nullable=True)

    __table_args__ = (
        Index(
            "ix_mal_student_field_time",
            "student_id",
            "field_name",
            "accessed_at",
        ),
    )
