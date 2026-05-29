"""
models/audit.py — 操作審計模型
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy import Column, ForeignKey, Integer, String, DateTime, Text, Index

from models.base import Base


class AuditLog(Base):
    """操作審計紀錄表"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_created", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_user", "user_id"),
        Index("ix_audit_logs_ack_created", "acknowledged_at", "created_at"),
        Index("ix_audit_session", "session_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, comment="操作者 user_id")
    username = Column(String(50), nullable=True, comment="操作者名稱")
    action = Column(String(20), nullable=False, comment="CREATE / UPDATE / DELETE")
    entity_type = Column(String(50), nullable=False, comment="資源類型")
    entity_id = Column(String(50), nullable=True, comment="資源 ID")
    summary = Column(Text, nullable=True, comment="操作摘要")
    changes = Column(
        Text,
        nullable=True,
        comment="變更內容 JSON（{before, after} 或 {created, deleted}）",
    )
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=now_taipei_naive, nullable=False)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True, comment="ack 時間")
    acknowledged_by = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="ack 操作者",
    )
    user_agent_hash = Column(
        String(64),
        nullable=True,
        comment="SHA256(UA) hex digest 取前 32 字元（避免直存 device PII；String(64) 為未來擴充保留）",
    )
    session_id = Column(
        String(64),
        nullable=True,
        comment="JWT jti claim — forensic 用，stateless 無伺服端 session",
    )
