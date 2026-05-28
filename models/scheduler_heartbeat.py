"""scheduler_heartbeats — DB-backed heartbeat for 14 scheduler iteration sites.

PK = scheduler_name；無 FK；純 ops 表。每次 scheduler tick 結尾 UPDATE
last_success_at；/health/schedulers 用 expected_interval_seconds 算 lag。

In-memory metrics (utils/scheduler_observability) 仍保留作 per-process 觀測；
DB heartbeat 解決 process restart 丟失問題（zeabur 重新部署、worker recycle
都不再丟「最近一次成功時間」訊息）。

注意：security_gc_scheduler 內部有 rate_limit_gc 與 jwt_blocklist_gc 兩個獨立
scheduler_iteration call site（不同 interval），對應兩列 heartbeat，並非單一
scheduler 名稱「security_gc」。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class SchedulerHeartbeat(Base):
    __tablename__ = "scheduler_heartbeats"

    scheduler_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    last_rows_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
