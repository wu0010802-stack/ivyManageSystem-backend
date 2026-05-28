"""services/line_token_health_scheduler.py — Phase 4 P1 resilience.

每日 08:00 Asia/Taipei ping /v2/bot/info；200 → healthy=true；
401/403 → healthy=false + Sentry alert（consecutive_failures milestone dedup 1/8/30）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from config import settings
from models.base import get_session_factory
from models.integration_health import LineTokenHealth
from utils.external_calls import tagged_capture

logger = logging.getLogger(__name__)

_BOT_INFO_URL = "https://api.line.me/v2/bot/info"
_DEDUP_MILESTONES = (1, 8, 30)


def tick_line_token_health() -> dict:
    """Ping LINE /v2/bot/info，更新 line_token_health singleton row。"""
    if not settings.line.channel_access_token:
        return {"skipped": "no_token"}

    metric: dict = {"healthy": False, "error": None}
    try:
        resp = requests.get(
            _BOT_INFO_URL,
            headers={"Authorization": f"Bearer {settings.line.channel_access_token}"},
            timeout=5,
        )
        if resp.status_code == 200:
            _update_health(healthy=True, error=None, reset_failures=True)
            metric["healthy"] = True
        elif resp.status_code in (401, 403):
            _update_health(healthy=False, error=f"http_{resp.status_code}", increment=True)
            _alert_if_milestone(f"http_{resp.status_code}", level="error")
            metric["error"] = f"http_{resp.status_code}"
        else:
            _update_health(
                healthy=False, error=f"http_{resp.status_code}", increment=True
            )
            metric["error"] = f"http_{resp.status_code}"
    except Exception as exc:
        _update_health(healthy=False, error=type(exc).__name__, increment=True)
        tagged_capture(exc, tag="line", level="warning")
        metric["error"] = type(exc).__name__
    return metric


def _update_health(
    *,
    healthy: bool,
    error: str | None,
    increment: bool = False,
    reset_failures: bool = False,
) -> None:
    """Upsert line_token_health singleton (id=1)."""
    session = get_session_factory()()
    try:
        row = session.query(LineTokenHealth).filter(LineTokenHealth.id == 1).first()
        now = datetime.now(timezone.utc)
        if row is None:
            row = LineTokenHealth(
                id=1,
                last_check_at=now,
                healthy=healthy,
                last_error=error,
                consecutive_failures=1 if increment else 0,
            )
            session.add(row)
        else:
            row.last_check_at = now
            row.healthy = healthy
            row.last_error = error
            if reset_failures:
                row.consecutive_failures = 0
            elif increment:
                row.consecutive_failures = (row.consecutive_failures or 0) + 1
        session.commit()
    finally:
        session.close()


def _alert_if_milestone(error: str, *, level: str = "error") -> None:
    """consecutive_failures 達里程碑時才 alert（dedup）。"""
    session = get_session_factory()()
    try:
        row = session.query(LineTokenHealth).filter(LineTokenHealth.id == 1).first()
        if row is None:
            return
        if row.consecutive_failures in _DEDUP_MILESTONES:
            tagged_capture(
                RuntimeError(
                    f"LINE token unhealthy: {error} "
                    f"(consecutive_failures={row.consecutive_failures})"
                ),
                tag="line",
                level=level,
            )
    finally:
        session.close()


async def run_line_token_health_scheduler(stop_event: asyncio.Event) -> None:
    """每日 token_health_ping_hour_taipei 整點 tick；stop_event 觸發時結束。"""
    while not stop_event.is_set():
        now_taipei = datetime.now(ZoneInfo("Asia/Taipei"))
        target_hour = getattr(settings.line, "token_health_ping_hour_taipei", 8)
        next_run = now_taipei.replace(
            hour=target_hour, minute=0, second=0, microsecond=0
        )
        if next_run <= now_taipei:
            next_run += timedelta(days=1)
        wait = (next_run - now_taipei).total_seconds()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            tick_line_token_health()
        except Exception as exc:
            logger.exception("line_token_health tick failed")
            tagged_capture(exc, tag="line", level="warning")
