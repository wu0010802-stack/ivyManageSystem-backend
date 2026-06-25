"""services/notification/retry_scheduler.py — LINE retry scheduler tick.

對應 spec §6.4。每 5 min 撈 NotificationLog.line_next_retry_at <= now() AND
line_retry_count < 3 的 row → 用 LINE_HANDLERS 重新 render + 重發。

Backoff：30s → 5min → 30min（指數）。第 3 次失敗 mark final=true。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from models.base import get_session_factory
from models.database import NotificationLog
from services.notification.dispatch import (
    PendingEvent,
    _get_line_adapter,
    _resolve_line_user_id,
)
from services.notification.renderers import render
from utils.external_calls import tagged_capture
from utils.scheduler_observability import scheduler_iteration

logger = logging.getLogger(__name__)

_BACKOFF_SECONDS = [30, 300, 1800]  # tick 0→1, 1→2, 2→final
_MAX_RETRIES = 3
_TICK_LIMIT = 100  # 單 tick 上限
_TICK_INTERVAL = 300  # 5 分鐘


def tick_line_retry(now_provider=lambda: datetime.now(timezone.utc)) -> dict:
    """每 5 分鐘 tick：撈 pending LINE retry 重發，回 metric dict."""
    session = get_session_factory()()
    metric = {"attempted": 0, "succeeded": 0, "failed": 0, "final_failed": 0}
    try:
        now = now_provider()
        # 先取候選 id（不鎖），再逐筆 re-lock + 處理 + commit。
        # 逐筆短交易：每筆成功狀態即時落地，進程在 tick 中途崩潰 / 被部署重啟時，
        # 已送出的 row 不會在下個 tick 重送（避免家長收重複 LINE 通知）。
        candidate_ids = [
            r_id
            for (r_id,) in (
                session.query(NotificationLog.id)
                .filter(
                    NotificationLog.line_next_retry_at.is_not(None),
                    NotificationLog.line_next_retry_at <= now,
                    NotificationLog.line_retry_count < _MAX_RETRIES,
                )
                .order_by(NotificationLog.id)
                .limit(_TICK_LIMIT)
                .all()
            )
        ]
        session.rollback()  # 結束唯讀候選查詢交易，後續每筆走獨立短交易

        for row_id in candidate_ids:
            # 單列 claim（skip_locked）+ 合格性 re-check 折進同一 SQL filter（DB 層比較，
            # 跨 SQLite/PG 安全）：row is None 同時涵蓋「他 worker 鎖住」與「候選成形後
            # 已被推進而不再合格」兩種情形，皆跳過不重送。逐筆 commit 即釋放，不持整批鎖。
            row = (
                session.query(NotificationLog)
                .filter(
                    NotificationLog.id == row_id,
                    NotificationLog.line_next_retry_at.is_not(None),
                    NotificationLog.line_next_retry_at <= now,
                    NotificationLog.line_retry_count < _MAX_RETRIES,
                )
                .with_for_update(skip_locked=True)
                .first()
            )
            if row is None:
                continue

            metric["attempted"] += 1
            try:
                ok = _retry_line_push(session, row)
                if ok is True:
                    row.line_next_retry_at = None
                    row.channels_succeeded = list(row.channels_succeeded) + [
                        "line(retry)"
                    ]
                    metric["succeeded"] += 1
                elif ok is None:
                    # 用戶不可達，已在 _retry_line_push 內 finalize
                    metric["final_failed"] += 1
                else:
                    _schedule_next_or_final(row, now)
                    if row.line_retry_count >= _MAX_RETRIES:
                        metric["final_failed"] += 1
                    else:
                        metric["failed"] += 1
            except Exception as exc:
                logger.exception("tick_line_retry row=%s failed", row.id)
                tagged_capture(exc, tag="line", level="error")
                _schedule_next_or_final(row, now)
                metric["failed"] += 1
            session.commit()  # 本筆狀態即時落地 + 釋放鎖
    finally:
        session.close()
    return metric


def _retry_line_push(session, row: NotificationLog) -> bool | None:
    """Reconstruct PendingEvent 從 log row + 重發 LINE.

    Returns:
      True  — 成功（LINE adapter 沒拋）
      False — LINE 仍失敗（應繼續 backoff / schedule_next_or_final）
      None  — 用戶不可達（已在此 finalize，caller 不再呼叫 schedule_next_or_final）
    """
    line_user_id = _resolve_line_user_id(session, row.recipient_user_id)
    if line_user_id is None:
        # 用戶不再可達（unfollow/inactive）— mark final，停止 retry
        row.line_retry_count = _MAX_RETRIES
        row.line_next_retry_at = None
        return None  # 已 finalize，caller 不再呼叫 schedule_next_or_final

    evt = PendingEvent(
        event_type=row.event_type,
        recipient_user_id=line_user_id,
        context=dict(row.payload_json),
        sender_id=row.sender_id,
        source_entity_type=row.source_entity_type,
        source_entity_id=row.source_entity_id,
        channels=("line",),
        line_group_id=None,
    )
    rendered = render(row.event_type, row.payload_json)
    try:
        _get_line_adapter().send(evt, rendered, log_id=row.id)
        return True
    except Exception:
        return False


def _schedule_next_or_final(row: NotificationLog, now: datetime) -> None:
    """更新 retry_count；若達 max mark final 並寫 channels_failed."""
    row.line_retry_count += 1
    if row.line_retry_count >= _MAX_RETRIES:
        row.line_next_retry_at = None
        failed = list(row.channels_failed)
        failed.append({"channel": "line", "error": "max_retries", "final": True})
        row.channels_failed = failed
    else:
        # backoff index：第 1 次失敗用 BACKOFF[0]=30s（首發 schedule 已用），
        # tick 後 line_retry_count=1 用 BACKOFF[1]=300s，tick 後 =2 用 BACKOFF[2]=1800s
        delay = _BACKOFF_SECONDS[min(row.line_retry_count, len(_BACKOFF_SECONDS) - 1)]
        row.line_next_retry_at = now + timedelta(seconds=delay)


async def run_line_retry_scheduler(stop_event: asyncio.Event) -> None:
    """非同步 wrapper：每 5 分鐘呼叫 tick_line_retry()。

    複用 leave_quota_expiry / graduation_scheduler asyncio polling pattern。
    """
    logger.info("line retry scheduler 啟動 (interval=%ss)", _TICK_INTERVAL)
    while not stop_event.is_set():
        with scheduler_iteration(
            "notification_retry",
            expected_interval_seconds=_TICK_INTERVAL,
        ):
            metric = await asyncio.to_thread(tick_line_retry)
            if metric["attempted"] > 0:
                logger.info("line retry tick: %s", metric)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_TICK_INTERVAL)
        except asyncio.TimeoutError:
            pass
