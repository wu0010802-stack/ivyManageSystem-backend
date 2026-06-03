"""models/scheduler_watermark.py — 排程器時間游標持久化。

部分排程器（如 announcement publish）以時間游標決定「上次處理到哪」。
游標若只存記憶體，重啟即重置 → 重啟/部署窗口內排程的工作會被永久跳過。
此表把游標落 DB，重啟後 seed 回放。每個排程器一列（name 為主鍵）。
"""

from sqlalchemy import Column, String, DateTime

from models.base import Base
from utils.taipei_time import now_taipei_naive


class SchedulerWatermark(Base):
    """排程器時間游標（每個排程器一列）。"""

    __tablename__ = "scheduler_watermarks"

    name = Column(String(64), primary_key=True, comment="排程器識別名")
    last_run_at = Column(DateTime, nullable=True, comment="上次處理到的時間游標")
    updated_at = Column(DateTime, default=now_taipei_naive, onupdate=now_taipei_naive)
