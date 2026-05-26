"""tests/test_leave_quota_expiry_scheduler.py — 補休到期 + 特休週年 scheduler 單元測試。

注意：pytest-asyncio 未安裝，async 測試改用 asyncio.run() 包在同步函式內執行。
"""

import asyncio

import pytest

from services.leave_quota_expiry_scheduler import (
    run_leave_quota_expiry_scheduler,
    scheduler_enabled,
)


def test_scheduler_enabled_default_false(monkeypatch):
    monkeypatch.delenv("LEAVE_QUOTA_EXPIRY_ENABLED", raising=False)
    from config import get_settings

    get_settings.cache_clear()
    assert scheduler_enabled() is False


def test_scheduler_stops_on_event(monkeypatch):
    """stop_event set → loop 結束（用 asyncio.run 執行，不依賴 pytest-asyncio）"""
    monkeypatch.setenv("LEAVE_QUOTA_EXPIRY_ENABLED", "true")
    monkeypatch.setenv("LEAVE_QUOTA_EXPIRY_CHECK_INTERVAL", "1")
    from config import get_settings

    get_settings.cache_clear()

    async def _run():
        stop = asyncio.Event()
        task = asyncio.create_task(run_leave_quota_expiry_scheduler(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run())
