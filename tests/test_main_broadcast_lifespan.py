"""驗證 app_lifespan startup/shutdown 有呼叫 get_broadcast().start()/stop()。"""

import asyncio
from unittest.mock import AsyncMock

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

    with TestClient(app) as _client:
        started.assert_awaited_once()

    stopped.assert_awaited_once()
