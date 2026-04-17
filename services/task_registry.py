"""輕量級 in-memory 背景任務狀態追蹤。

提供一個 thread-safe 的任務註冊表，供 FastAPI BackgroundTasks 回報長時間任務
（例如大型 Excel 匯出、爬蟲、批次薪資計算）的進度、狀態與結果檔案位置。

設計原則：
  - 單機部署夠用；多機部署應改 Redis / DB 支援。
  - 任務狀態約 1 小時後自動清理，避免無上限增長。
  - 只保留公開的 API（create/update/get/list/prune），內部結構不暴露。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskRecord:
    task_id: str
    kind: str  # 任務分類，如 "export_employees"、"moe_scrape"
    status: TaskStatus = TaskStatus.QUEUED
    progress: float = 0.0  # 0.0 ~ 1.0
    message: str = ""
    result: Optional[Any] = None  # 可填結果檔案路徑、摘要等
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class TaskRegistry:
    """Thread-safe 任務狀態暫存。"""

    def __init__(self, retention_seconds: int = 3600):
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.RLock()
        self._retention = retention_seconds

    def create(self, kind: str) -> TaskRecord:
        task_id = uuid.uuid4().hex
        record = TaskRecord(task_id=task_id, kind=kind)
        with self._lock:
            self._tasks[task_id] = record
            self._prune_locked()
        return record

    def update(
        self,
        task_id: str,
        *,
        status: Optional[TaskStatus] = None,
        progress: Optional[float] = None,
        message: Optional[str] = None,
        result: Optional[Any] = None,
        error: Optional[str] = None,
    ) -> Optional[TaskRecord]:
        with self._lock:
            record = self._tasks.get(task_id)
            if not record:
                return None
            if status is not None:
                record.status = status
            if progress is not None:
                record.progress = max(0.0, min(1.0, progress))
            if message is not None:
                record.message = message
            if result is not None:
                record.result = result
            if error is not None:
                record.error = error
            record.updated_at = time.time()
            return record

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._tasks.get(task_id)

    def to_dict(self, record: TaskRecord) -> dict:
        data = asdict(record)
        # enum -> str
        data["status"] = record.status.value
        return data

    def list(self, kind: Optional[str] = None) -> list[TaskRecord]:
        with self._lock:
            return [r for r in self._tasks.values() if kind is None or r.kind == kind]

    def prune(self) -> int:
        with self._lock:
            return self._prune_locked()

    # ── internal ────────────────────────────────────────────────────────
    def _prune_locked(self) -> int:
        cutoff = time.time() - self._retention
        removed = [tid for tid, r in self._tasks.items() if r.updated_at < cutoff]
        for tid in removed:
            del self._tasks[tid]
        return len(removed)


# 全域 singleton（單機環境）
_default_registry = TaskRegistry()


def get_task_registry() -> TaskRegistry:
    """取得全域 TaskRegistry 實例。"""
    return _default_registry
