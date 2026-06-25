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
        # 先取候選 id（不鎖），再逐筆 re-lock + 處理 + commit。
        # 逐筆短交易：每筆成功狀態即時落地，進程在 tick 中途崩潰 / 被部署重啟時，
        # 已上傳的 row 不會在下個 tick 重複上傳。attempted 仍以「候選列數」計
        # （維持 backend 非 Supabase 時 short-circuit 仍回報撈到列數的既有語義）。
        candidate_ids = [
            r_id
            for (r_id,) in (
                session.query(PendingUpload.id)
                .filter(
                    PendingUpload.succeeded_at.is_(None),
                    PendingUpload.next_retry_at <= now,
                    PendingUpload.attempts < _MAX_ATTEMPTS,
                )
                .order_by(PendingUpload.id)
                .limit(_TICK_LIMIT)
                .all()
            )
        ]
        session.rollback()  # 結束唯讀候選查詢交易
        metric["attempted"] = len(candidate_ids)

        from utils.storage import get_backend

        backend = get_backend()
        # local 模式不會走這 scheduler，但安全 check
        if backend.__class__.__name__ != "SupabaseStorage":
            return metric

        for row_id in candidate_ids:
            # 單列 claim（skip_locked）+ 合格性 re-check 折進同一 SQL filter（DB 層比較，
            # 跨 SQLite/PG 安全）：row is None 同時涵蓋「他 worker 鎖住」與「候選成形後
            # 已被推進」兩種情形，皆跳過不重傳。
            row = (
                session.query(PendingUpload)
                .filter(
                    PendingUpload.id == row_id,
                    PendingUpload.succeeded_at.is_(None),
                    PendingUpload.next_retry_at <= now,
                    PendingUpload.attempts < _MAX_ATTEMPTS,
                )
                .with_for_update(skip_locked=True)
                .first()
            )
            if row is None:
                continue
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
            session.commit()  # 本筆狀態即時落地 + 釋放鎖
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
            await asyncio.to_thread(tick_pending_uploads)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TICK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
