"""services/notification/pending_uploads_scheduler.py — Phase 4 P1 resilience.

每 5 min 撈 pending_uploads.next_retry_at<=now() AND attempts<5 → 重 push to Supabase。
backoff: 30s → 2min → 10min → 1hr → 6hr（5 次失敗後 mark final + Sentry alert）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from models.base import get_session_factory
from models.pending_uploads import PendingUpload
from utils.external_calls import tagged_capture
from utils.scheduler_observability import scheduler_iteration

logger = logging.getLogger(__name__)

_BACKOFF_SECONDS = [30, 120, 600, 3600, 21600]
_MAX_ATTEMPTS = 5
_TICK_LIMIT = 50
TICK_INTERVAL_SECONDS = 300  # 5 min


def tick_pending_uploads(now_provider=lambda: datetime.now(timezone.utc)) -> dict:
    """撈待補傳 row，推 Supabase；回 metric dict。"""
    session = get_session_factory()()
    metric = {"attempted": 0, "succeeded": 0, "failed": 0, "final_failed": 0}
    try:
        now = now_provider()
        rows = (
            session.query(PendingUpload)
            .filter(
                PendingUpload.succeeded_at.is_(None),
                PendingUpload.next_retry_at <= now,
                PendingUpload.attempts < _MAX_ATTEMPTS,
            )
            .limit(_TICK_LIMIT)
            # 多 worker 時避免兩個 worker 撈到同批 pending_upload 而重複上傳。
            # SQLite 測試環境會忽略 FOR UPDATE；PostgreSQL 會用 SKIP LOCKED claim row。
            .with_for_update(skip_locked=True)
            .all()
        )
        metric["attempted"] = len(rows)

        from utils.storage import get_backend

        backend = get_backend()
        # local 模式不會走這 scheduler，但安全 check
        if backend.__class__.__name__ != "SupabaseStorage":
            return metric

        for row in rows:
            try:
                with open(row.local_path, "rb") as f:
                    data = f.read()
                # 直接呼叫 SDK 避免無限 fallback 迴圈
                from utils.supabase_storage import _resolve_bucket

                bucket = backend._client.storage.from_(_resolve_bucket(row.module))
                bucket.upload(
                    path=row.key,
                    file=data,
                    file_options={"content-type": row.content_type, "upsert": "true"},
                )
                row.succeeded_at = now
                # cleanup local file
                try:
                    os.remove(row.local_path)
                except OSError:
                    pass
                metric["succeeded"] += 1
            except Exception as exc:
                logger.exception("pending_upload row=%s failed", row.id)
                row.attempts += 1
                if row.attempts >= _MAX_ATTEMPTS:
                    # 永久失敗：附件（成長報告 / IEP / 家長上傳）重試 5 次後將遺失。
                    # 升級為 error 級，避免與一般可重試失敗同被當 warning 靜默吞掉
                    # （health 端點另有 supabase.final_failed gauge 一併曝光）。
                    row.last_error = f"final: {exc!s}"[:500]
                    metric["final_failed"] += 1
                    tagged_capture(exc, tag="supabase", level="error")
                else:
                    delay = _BACKOFF_SECONDS[
                        min(row.attempts, len(_BACKOFF_SECONDS) - 1)
                    ]
                    row.next_retry_at = now + timedelta(seconds=delay)
                    row.last_error = str(exc)[:500]
                    metric["failed"] += 1
                    tagged_capture(exc, tag="supabase", level="warning")
        session.commit()
    finally:
        session.close()
    return metric


async def run_pending_uploads_scheduler(stop_event: asyncio.Event) -> None:
    """每 TICK_INTERVAL_SECONDS 秒執行一次 tick；stop_event 觸發時結束。"""
    while not stop_event.is_set():
        with scheduler_iteration(
            "pending_uploads",
            expected_interval_seconds=TICK_INTERVAL_SECONDS,
        ):
            tick_pending_uploads()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TICK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
