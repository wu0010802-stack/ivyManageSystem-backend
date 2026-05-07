"""政府開放資料同步相關 ORM model。

對應 migration `20260507_05df4844e040_gov_data_sync.py`。
不直接放 models/config.py 是為了避免該檔繼續膨脹。
"""

from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)

from models.base import Base


class GovDataSnapshot(Base):
    """單次拉取單一政府資料源的原始 JSON 快照。

    成功與失敗都落地一筆；hash 用於跳過重複 fetch。
    """

    __tablename__ = "gov_data_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(40), nullable=False)
    source_url = Column(String(500), nullable=False)
    fetched_at = Column(DateTime, server_default=func.now(), nullable=False)
    http_status = Column(Integer, nullable=False)
    raw_payload = Column(JSON, nullable=True)
    payload_hash = Column(String(64), nullable=False)
    error = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_gov_snapshot_source_time", "source", "fetched_at"),
        Index("ix_gov_snapshot_payload_hash", "payload_hash"),
    )


class InsuranceBracketsStaging(Base):
    """合成後待審核的勞健保級距版本。"""

    __tablename__ = "insurance_brackets_staging"

    id = Column(Integer, primary_key=True, autoincrement=True)
    effective_year = Column(Integer, nullable=False)
    composed_at = Column(DateTime, server_default=func.now(), nullable=False)
    composed_from = Column(JSON, nullable=False)
    brackets = Column(JSON, nullable=False)
    rates = Column(JSON, nullable=False)
    diff_summary = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False, server_default="pending")
    decided_by = Column(String(50), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    decision_reason = Column(String(500), nullable=True)

    __table_args__ = (Index("ix_staging_year_status", "effective_year", "status"),)


class MinimumWageHistory(Base):
    """基本工資歷史；取代 services/salary/minimum_wage.py 的兩個常數。"""

    __tablename__ = "minimum_wage_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    effective_date = Column(Date, nullable=False)
    monthly = Column(Integer, nullable=False)
    hourly = Column(Integer, nullable=False)
    source_snapshot_id = Column(
        Integer, ForeignKey("gov_data_snapshots.id"), nullable=True
    )
    confirmed_by = Column(String(50), nullable=False)
    confirmed_at = Column(DateTime, server_default=func.now(), nullable=False)
    confirm_reason = Column(String(500), nullable=False)

    __table_args__ = (
        UniqueConstraint("effective_date", name="uq_minimum_wage_effective_date"),
    )


class MinimumWageStaging(Base):
    """待審核的基本工資版本。"""

    __tablename__ = "minimum_wage_staging"

    id = Column(Integer, primary_key=True, autoincrement=True)
    effective_date = Column(Date, nullable=False)
    monthly = Column(Integer, nullable=False)
    hourly = Column(Integer, nullable=False)
    source_snapshot_id = Column(
        Integer, ForeignKey("gov_data_snapshots.id"), nullable=False
    )
    composed_at = Column(DateTime, server_default=func.now(), nullable=False)
    status = Column(String(20), nullable=False, server_default="pending")
    decided_by = Column(String(50), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    decision_reason = Column(String(500), nullable=True)
