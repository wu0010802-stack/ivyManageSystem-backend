"""主事件迴圈註冊（main loop registry）。

Why: sync 路由在 FastAPI 的 threadpool 內執行，沒有 running loop。
若 sync 路由想做 WS 廣播（fire-and-forget），直接 `asyncio.run(...)`
會新建一個 loop；但 WS 連線物件已綁定主 loop 的 transport，
新 loop 上呼叫 `WebSocket.send_*` 會誤判 transport 已死 → 把訂閱者
從 channel 踢掉，造成所有後續事件收不到。

正確做法：lifespan 啟動時把主 loop 物件存進此 registry，sync 端
透過 `asyncio.run_coroutine_threadsafe(coro, get_main_loop())`
把任務丟回主 loop 排程，確保 WS 操作在主 loop 內完成。
"""

from __future__ import annotations

import asyncio
from typing import Optional

_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """由 FastAPI lifespan 在啟動時呼叫一次。"""
    global _main_loop
    _main_loop = loop


def get_main_loop() -> Optional[asyncio.AbstractEventLoop]:
    """取得主事件迴圈；尚未註冊或已關閉時回 None。"""
    if _main_loop is None or _main_loop.is_closed():
        return None
    return _main_loop


def clear_main_loop() -> None:
    """測試用：清除註冊（避免測試間殘留）。"""
    global _main_loop
    _main_loop = None
