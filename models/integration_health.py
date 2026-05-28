"""models/integration_health.py — Phase 4 P1 resilience：外部整合健康狀態 row.

LineTokenHealth singleton row（id=1）：每日 daily tick + call-site 401/403 共寫。
"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from models.base import Base


class LineTokenHealth(Base):
    __tablename__ = "line_token_health"

    id = Column(Integer, primary_key=True)
    last_check_at = Column(DateTime(timezone=True), nullable=False)
    healthy = Column(Boolean, nullable=False)
    last_error = Column(String(200), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
