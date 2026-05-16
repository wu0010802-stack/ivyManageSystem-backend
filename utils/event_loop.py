"""主事件迴圈註冊：給 sync 路由（thread pool）做 WS 廣播時跨 thread 調度用。

機制：
- FastAPI lifespan 啟動時把 asyncio.get_running_loop() 註冊到此模組
- sync def 路由執行在 starlette thread pool（沒有自己的 loop）
- 需要 await 廣播時用 asyncio.run_coroutine_threadsafe(_fanout(), get_main_loop())
  把 coroutine 投回主 loop，避免在 thread 內 asyncio.run() 起新 loop ——
  WS transport 綁主 loop，新 loop 的 broadcast 會被視為僵死並 unsubscribe
  所有訂閱者（B1-B4 round 4 bug）。
"""

from __future__ import annotations

import asyncio

_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def get_main_loop() -> asyncio.AbstractEventLoop | None:
    return _main_loop
