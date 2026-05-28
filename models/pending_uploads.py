"""models/pending_uploads.py — Phase 4 P1 resilience：Supabase 上傳失敗暫存 row.

scheduler 每 5 min 撈 attempts<5 AND next_retry_at<=now() 重推 Supabase。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text, func

from models.base import Base


class PendingUpload(Base):
    __tablename__ = "pending_uploads"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    module = Column(String(40), nullable=False)
    key = Column(String(255), nullable=False)
    content_type = Column(String(80), nullable=False)
    local_path = Column(String(500), nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    next_retry_at = Column(DateTime(timezone=True), nullable=False)
    last_error = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    succeeded_at = Column(DateTime(timezone=True), nullable=True)
