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

# access_type 列舉：讓 §6 取用稽核可結構化區分「被動顯示」與「具理由取用」，
# 不必靠 reason 字串比對（後者脆弱）。passive 為 server_default（多數寫入點皆被動）。
MEDICAL_ACCESS_PASSIVE = "passive"  # 詳細頁/清單/家長端被動回出醫療欄位（無顯式理由）
MEDICAL_ACCESS_EXPLICIT = "explicit"  # reason-gated /medical 端點具理由取用（≥10 字）


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
        # SET NULL：硬刪 student 時保留 §6 醫療取用稽核（RA-MED-9），與上方 user_id 一致
        ForeignKey("students.id", ondelete="SET NULL"),
        nullable=True,
    )
    field_name = Column(
        String(50),
        nullable=False,
        comment="allergy / medication / special_needs / temperature_c / bundle",
    )
    reason = Column(Text, nullable=False, comment="取用理由（endpoint 層 ≥10 字 gate）")
    access_type = Column(
        String(20),
        nullable=False,
        server_default=MEDICAL_ACCESS_PASSIVE,
        comment="passive=被動顯示(詳細頁/清單/家長端,無顯式理由) / explicit=具理由取用(/medical)",
    )
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
