"""薪資批次計算 async job registry（DB-backed）。

提供：
- 建立 job（回傳 job_id）
- 回報進度（done/total/current_employee）
- 紀錄結果（results, errors, finished_at）
- 查詢狀態 / 找同月份 active job

設計：
- 狀態儲存於 `salary_calc_jobs` 表（migration 20260419_l0g1h2i3j4k5）
- 介面保持與原 in-process registry 相同（create / get / find_active /
  mark_running / update_progress / complete / fail）
- results / errors 僅在 complete() 時 JSON serialize 一次寫入，避免每筆員工
  都更新巨量欄位
- TTL：完成超過 _JOB_TTL_SEC 秒的 job 會在下次 get / find_active / create 時
  被 evict；可避免表無限成長

多 worker：由於狀態在共用 DB，任一 worker 皆能查到其他 worker 建立的 job，
find_active() 真正能跨 worker 防止重複觸發。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import desc

from models.base import session_scope
from models.database import SalaryCalcJobRecord

_JOB_TTL_SEC = 3600


@dataclass
class SalaryCalcJob:
    """Registry 回傳給呼叫端的 view（從 DB row 物化）。

    保留 dataclass 介面以維持既有呼叫端與測試相容。
    """

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

    @classmethod
    def _from_record(cls, r: SalaryCalcJobRecord) -> "SalaryCalcJob":
        return cls(
            job_id=r.job_id,
            year=r.year,
            month=r.month,
            total=r.total or 0,
            status=r.status,
            done=r.done or 0,
            current_employee=r.current_employee or "",
            results=json.loads(r.results_json) if r.results_json else [],
            errors=json.loads(r.errors_json) if r.errors_json else [],
            error_message=r.error_message,
            created_at=r.created_at.timestamp() if r.created_at else time.time(),
            started_at=r.started_at.timestamp() if r.started_at else None,
            finished_at=r.finished_at.timestamp() if r.finished_at else None,
        )


class _SalaryJobRegistry:
    def __init__(self):
        # Lock 保留（對同進程多 thread 的 create / evict 競態保守保護）
        self._lock = threading.Lock()

    def create(self, year: int, month: int, total: int) -> SalaryCalcJob:
        job_id = uuid.uuid4().hex
        with self._lock:
            with session_scope() as s:
                self._evict_expired(s)
                record = SalaryCalcJobRecord(
                    job_id=job_id,
                    year=year,
                    month=month,
                    status="pending",
                    total=total,
                    done=0,
                    current_employee="",
                )
                s.add(record)
                s.flush()
                return SalaryCalcJob._from_record(record)

    def get(self, job_id: str) -> Optional[SalaryCalcJob]:
        with session_scope() as s:
            self._evict_expired(s)
            r = (
                s.query(SalaryCalcJobRecord)
                .filter(SalaryCalcJobRecord.job_id == job_id)
                .first()
            )
            return SalaryCalcJob._from_record(r) if r else None

    def find_active(self, year: int, month: int) -> Optional[SalaryCalcJob]:
        """回傳同 year/month 仍在 pending/running 的 job（若有）；否則 None。

        DB-backed 後此方法跨 worker 生效：任一 worker 查詢都看到其他 worker
        建立的 active job，真正阻擋跨 worker 重複觸發。
        """
        with session_scope() as s:
            self._evict_expired(s)
            r = (
                s.query(SalaryCalcJobRecord)
                .filter(
                    SalaryCalcJobRecord.year == year,
                    SalaryCalcJobRecord.month == month,
                    SalaryCalcJobRecord.status.in_(("pending", "running")),
                )
                .order_by(desc(SalaryCalcJobRecord.created_at))
                .first()
            )
            return SalaryCalcJob._from_record(r) if r else None

    def mark_running(self, job_id: str) -> None:
        with session_scope() as s:
            r = (
                s.query(SalaryCalcJobRecord)
                .filter(SalaryCalcJobRecord.job_id == job_id)
                .first()
            )
            if r:
                r.status = "running"
                r.started_at = datetime.now()

    def update_progress(self, job_id: str, done: int, total: int, current: str) -> None:
        with session_scope() as s:
            r = (
                s.query(SalaryCalcJobRecord)
                .filter(SalaryCalcJobRecord.job_id == job_id)
                .first()
            )
            if r:
                r.done = done
                r.total = total
                r.current_employee = current or ""

    def complete(self, job_id: str, results: list, errors: list) -> None:
        with session_scope() as s:
            r = (
                s.query(SalaryCalcJobRecord)
                .filter(SalaryCalcJobRecord.job_id == job_id)
                .first()
            )
            if r:
                r.results_json = json.dumps(results, default=str, ensure_ascii=False)
                r.errors_json = json.dumps(errors, default=str, ensure_ascii=False)
                r.status = "completed"
                r.finished_at = datetime.now()
                r.done = r.total or r.done

    def fail(self, job_id: str, message: str) -> None:
        with session_scope() as s:
            r = (
                s.query(SalaryCalcJobRecord)
                .filter(SalaryCalcJobRecord.job_id == job_id)
                .first()
            )
            if r:
                r.error_message = message
                r.status = "failed"
                r.finished_at = datetime.now()

    def _evict_expired(self, session) -> None:
        cutoff = datetime.now() - timedelta(seconds=_JOB_TTL_SEC)
        session.query(SalaryCalcJobRecord).filter(
            SalaryCalcJobRecord.finished_at.isnot(None),
            SalaryCalcJobRecord.finished_at < cutoff,
        ).delete(synchronize_session=False)

    def clear_all(self) -> None:
        """測試用：清空所有 job 紀錄（PRODUCTION 不應呼叫）。"""
        with session_scope() as s:
            s.query(SalaryCalcJobRecord).delete(synchronize_session=False)


registry = _SalaryJobRegistry()
