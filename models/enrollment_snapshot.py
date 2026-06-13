"""
models/enrollment_snapshot.py — 月度在籍人數快照

結算用的班級/全校在籍人數「看得到、改得了、鎖得住」（spec
2026-06-13-enrollment-count-correctness L2）：結薪前產生快照，HR 檢視/手調/
確認後，薪資引擎的獎金人數一律讀快照；無快照月份 fallback 即時計算。
歷史重算讀當時快照 → 永遠可重現；事後補登學生異動以重產 diff 浮出。
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)

from models.base import Base
from utils.taipei_time import now_taipei_naive


class ClassEnrollmentSnapshot(Base):
    """某月某班（或全校）的在籍人數快照。classroom_id NULL = 全校總數列。"""

    __tablename__ = "class_enrollment_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_year = Column(Integer, nullable=False)
    snapshot_month = Column(Integer, nullable=False)
    classroom_id = Column(
        Integer, ForeignKey("classrooms.id"), nullable=True, comment="NULL=全校總數"
    )
    # Numeric(6,1)：按日加權模式（L3）會出現小數人數
    student_count = Column(Numeric(6, 1), nullable=False)
    count_mode = Column(
        String(20),
        nullable=False,
        default="month_end",
        server_default="month_end",
        comment="month_end / daily_weighted / manual（手調）",
    )
    is_confirmed = Column(Boolean, nullable=False, default=False)
    confirmed_by = Column(String(50), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    adjust_reason = Column(String(200), nullable=True, comment="手調原因")
    updated_by = Column(String(50), nullable=True)
    generated_at = Column(DateTime, nullable=False, default=now_taipei_naive)
    updated_at = Column(
        DateTime, nullable=True, default=now_taipei_naive, onupdate=now_taipei_naive
    )

    __table_args__ = (
        # PG unique 視 NULL 各自相異，全校列（classroom_id NULL）的唯一性
        # 由 migration 的 partial unique index 補強；程式端一律 lookup-then-upsert。
        Index("ix_enrollment_snapshot_ym", "snapshot_year", "snapshot_month"),
    )
