"""薪資批次計算 async job registry（in-process）。

提供：
- 建立 job（回傳 job_id）
- 回報進度（done/total/current_employee）
- 紀錄結果（results, errors, finished_at）
- 查詢狀態

限制：
- 單 worker 記憶體，多實例部署需改 Redis-backed
- 結果 TTL 預設 1 小時後自動清除
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

_JOB_TTL_SEC = 3600


@dataclass
class SalaryCalcJob:
    job_id: str
    year: int
    month: int
    total: int
    status: str = "pending"  # pending, running, completed, failed
    done: int = 0
    current_employee: str = ""
    results: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    error_message: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "year": self.year,
            "month": self.month,
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "progress_ratio": (self.done / self.total) if self.total else 0.0,
            "current_employee": self.current_employee,
            "errors": self.errors,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result_count": len(self.results),
        }


class _SalaryJobRegistry:
    def __init__(self):
        self._jobs: dict[str, SalaryCalcJob] = {}
        self._lock = threading.Lock()

    def create(self, year: int, month: int, total: int) -> SalaryCalcJob:
        job = SalaryCalcJob(
            job_id=uuid.uuid4().hex,
            year=year,
            month=month,
            total=total,
        )
        with self._lock:
            self._evict_expired_locked()
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Optional[SalaryCalcJob]:
        with self._lock:
            self._evict_expired_locked()
            return self._jobs.get(job_id)

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = "running"
                job.started_at = time.time()

    def update_progress(self, job_id: str, done: int, total: int, current: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.done = done
                job.total = total
                job.current_employee = current

    def complete(self, job_id: str, results: list, errors: list) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.results = results
                job.errors = errors
                job.status = "completed"
                job.finished_at = time.time()
                job.done = job.total

    def fail(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.error_message = message
                job.status = "failed"
                job.finished_at = time.time()

    def _evict_expired_locked(self) -> None:
        now = time.time()
        expired = [
            jid
            for jid, job in self._jobs.items()
            if job.finished_at and (now - job.finished_at) > _JOB_TTL_SEC
        ]
        for jid in expired:
            self._jobs.pop(jid, None)


registry = _SalaryJobRegistry()
