"""
LINE 通知設定 Model
"""

from datetime import datetime
from utils.taipei_time import now_taipei_naive

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from models.base import Base
from utils.medical_field_type import EncryptedText


class LineConfig(Base):
    __tablename__ = "line_configs"

    id = Column(Integer, primary_key=True)
    # [C41] LINE 憑證機密：ORM 透明 Fernet 加密（DB 底層仍 Text；legacy 明文
    # passthrough 讀取）。對齊 models/portfolio.py StudentAllergy 醫療欄位做法。
    # 明文外洩即可冒名推播 / 偽造 webhook 簽名。
    channel_access_token = Column(EncryptedText, nullable=True)
    target_id = Column(String(100), nullable=True)  # group ID 或 user ID
    is_enabled = Column(Boolean, default=False)
    channel_secret = Column(EncryptedText, nullable=True)
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)
