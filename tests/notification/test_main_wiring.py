"""main.py lifespan 把 dispatch hook 綁到 production session factory + WS loop 已註冊。

整合測試：mock 掉 on_startup（含 alembic upgrade）避免 DB migration 副作用，
只驗証 dispatch.install_session_hooks 與 set_main_loop 的 lifespan wiring。
"""

import asyncio
from unittest.mock import patch, MagicMock

import pytest


def test_main_lifespan_installs_dispatch_hooks():
    """lifespan 應在 on_startup 之前呼叫 install_session_hooks(production_factory)。"""
    from main import app_lifespan, app
    from models.base import get_session_factory
    from services.notification import dispatch

    # clear sentinel 確保乾淨起點
    factory = get_session_factory()
    dispatch._HOOKS_INSTALLED.discard(factory)

    # mock on_startup 避免 alembic upgrade 副作用（DB migration 在 CI 已跑過）
    with (
        patch("main.on_startup", return_value=None),
        patch("main.run_alembic_upgrade", return_value=None, create=True),
    ):

        async def run():
            async with app_lifespan(app):
                pass

        asyncio.run(run())

    assert (
        factory in dispatch._HOOKS_INSTALLED
    ), "install_session_hooks 未在 lifespan 呼叫，production session factory 不在 _HOOKS_INSTALLED"


def test_main_lifespan_sets_main_loop():
    """lifespan 應在啟動時設定 main loop（WS 廣播所需）。"""
    from main import app_lifespan, app
    from utils.event_loop import get_main_loop

    with patch("main.on_startup", return_value=None):

        async def run():
            async with app_lifespan(app):
                assert get_main_loop() is not None, "set_main_loop 未在 lifespan 呼叫"

        asyncio.run(run())
