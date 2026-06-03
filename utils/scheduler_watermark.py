"""utils/scheduler_watermark.py — 排程器時間游標 get/set helper。

把排程器時間游標落 DB（models.SchedulerWatermark），讓游標跨重啟存活。

set_watermark 刻意**不 commit**：caller 須在同一事務內推進游標與業務寫入
（例如 enqueue 通知），讓兩者原子落地——否則崩潰在兩次 commit 之間會重做
已處理的工作（重複推播家長 LINE）。
"""

from __future__ import annotations

from datetime import datetime

from models.scheduler_watermark import SchedulerWatermark


def get_watermark(session, name: str) -> datetime | None:
    """讀取 name 對應的時間游標；未設定回 None。"""
    row = session.get(SchedulerWatermark, name)
    return row.last_run_at if row else None


def set_watermark(session, name: str, ts: datetime) -> None:
    """upsert name 的時間游標為 ts。不 commit（事務邊界由 caller 控制）。"""
    row = session.get(SchedulerWatermark, name)
    if row is None:
        session.add(SchedulerWatermark(name=name, last_run_at=ts))
    else:
        row.last_run_at = ts
