"""報表/儀表板快取模型。"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, Index

from models.base import Base


class ReportSnapshot(Base):
    """高成本報表快取快照。"""

    __tablename__ = "report_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cache_key = Column(String(255), unique=True, nullable=False, comment="快取鍵")
    category = Column(String(50), nullable=False, comment="報表類型")
    payload = Column(Text, nullable=False, comment="JSON 序列化內容")
    computed_at = Column(DateTime, default=datetime.now, nullable=False, comment="計算完成時間")
    expires_at = Column(DateTime, nullable=False, comment="快取失效時間")

    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    __table_args__ = (
        Index("ix_report_snapshots_category", "category"),
        Index("ix_report_snapshots_expires_at", "expires_at"),
    )
