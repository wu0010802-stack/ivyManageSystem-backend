"""
LINE 通知設定 Model
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from models.base import Base


class LineConfig(Base):
    __tablename__ = "line_configs"

    id = Column(Integer, primary_key=True)
    channel_access_token = Column(String(512), nullable=True)
    target_id = Column(String(100), nullable=True)  # group ID 或 user ID
    is_enabled = Column(Boolean, default=False)
    channel_secret = Column(String(256), nullable=True)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)
