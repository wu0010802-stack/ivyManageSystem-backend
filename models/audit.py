"""
models/audit.py — 操作審計模型
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, Text, Index

from models.base import Base


class AuditLog(Base):
    """操作審計紀錄表"""
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_created", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_user", "user_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, comment="操作者 user_id")
    username = Column(String(50), nullable=True, comment="操作者名稱")
    action = Column(String(20), nullable=False, comment="CREATE / UPDATE / DELETE")
    entity_type = Column(String(50), nullable=False, comment="資源類型")
    entity_id = Column(String(50), nullable=True, comment="資源 ID")
    summary = Column(Text, nullable=True, comment="操作摘要")
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
