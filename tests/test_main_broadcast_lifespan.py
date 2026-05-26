"""驗證 app_lifespan startup/shutdown 有呼叫 get_broadcast().start()/stop()。"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _reset_broadcast():
    from utils.broadcast import reset_for_tests

    reset_for_tests()
    yield
    reset_for_tests()


def test_lifespan_starts_and_stops_broadcast(monkeypatch):
    """startup 呼叫 backend.start(), shutdown 呼叫 stop()。"""
    monkeypatch.setenv("CACHE_BACKEND", "memory")
    from config import reset_for_tests as cfg_reset

    cfg_reset()

    started = AsyncMock()
    stopped = AsyncMock()

    from utils.broadcast import get_broadcast, reset_for_tests as br_reset

    br_reset()
    backend = get_broadcast()
    monkeypatch.setattr(backend, "start", started)
    monkeypatch.setattr(backend, "stop", stopped)

    from main import app

    # mock on_startup 避免 alembic upgrade 副作用（baseline migration
    # `4ddf3ebad3e8` 從空 DB 跑會 UndefinedTable；CI Tests step 已用
    # `Base.metadata.create_all` + `alembic stamp heads` 處理 schema，
    # 不需 lifespan 再跑 upgrade）。同 tests/notification/test_main_wiring.py pattern。
    with patch("main.on_startup", return_value=None):
        with TestClient(app) as _client:
            started.assert_awaited_once()

    stopped.assert_awaited_once()
