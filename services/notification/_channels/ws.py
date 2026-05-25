"""WS channel adapter：sync→async bridge via utils.event_loop.get_main_loop。

職責劃分：
- WsAdapter.send：只處理 parent.* 與 dismissal.created（broadcast_parent /
  dismissal_manager.broadcast）
- _inbox_ws_push：員工通知中心 realtime 推送，給 dispatch._fan_out 在 in_app
  路徑直接呼叫；不經 WsAdapter（兩者語意不同 — 員工 inbox 失敗不算 channel
  failure，只 warning）

兩者皆透過 asyncio.run_coroutine_threadsafe 把 coroutine 投回主 loop，避免在
threadpool worker 內起新 loop（會打死 WS transport，B1-B4 round 4 bug）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api.contact_book_ws import broadcast_parent
from api.dismissal_ws import manager as dismissal_manager
from utils.event_loop import get_main_loop

logger = logging.getLogger(__name__)

_WS_TIMEOUT_SECONDS = 2.0


def _build_payload(evt: Any, rendered: Any, log_id: int) -> dict[str, Any]:
    return {
        "event_type": evt.event_type,
        "title": rendered.title,
        "body": rendered.body,
        "deep_link": rendered.deep_link,
        "log_id": log_id,
    }


class WsAdapter:
    """非 inbox 的 WS 推送 channel（parent.* / dismissal.created）。"""

    def send(self, evt: Any, rendered: Any, *, log_id: int) -> None:
        loop = get_main_loop()
        if loop is None:
            raise RuntimeError("WS loop 未註冊（main.py lifespan 漏 set_main_loop）")
        coro = self._dispatch(evt, rendered, log_id)
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        fut.result(timeout=_WS_TIMEOUT_SECONDS)

    async def _dispatch(self, evt: Any, rendered: Any, log_id: int) -> None:
        payload = _build_payload(evt, rendered, log_id)
        if evt.event_type.startswith("parent."):
            await broadcast_parent(evt.recipient_user_id, payload)
        elif evt.event_type == "dismissal.created":
            classroom_id = evt.context.get("classroom_id")
            if classroom_id is None:
                raise ValueError("dismissal.created 缺 context['classroom_id']")
            await dismissal_manager.broadcast(classroom_id, payload)
        else:
            raise RuntimeError(
                f"ws channel 不支援 event_type={evt.event_type}；"
                "員工 inbox WS 應走 _inbox_ws_push 不經此 adapter"
            )


# ── 員工 inbox WS 推送：給 dispatch._fan_out 直接呼叫 ──


async def _inbox_ws_push_async(evt: Any, rendered: Any, log_id: int) -> None:
    from api.inbox_ws import inbox_broadcast_user

    payload = _build_payload(evt, rendered, log_id)
    await inbox_broadcast_user(evt.recipient_user_id, payload)


def _inbox_ws_push(evt: Any, rendered: Any, log_id: int) -> None:
    """同步 wrapper，給 _fan_out 在 in_app 路徑呼叫。

    失敗由 caller swallow（不影響 channels_succeeded）。
    """
    loop = get_main_loop()
    if loop is None:
        raise RuntimeError("WS loop 未註冊")
    fut = asyncio.run_coroutine_threadsafe(
        _inbox_ws_push_async(evt, rendered, log_id), loop
    )
    fut.result(timeout=_WS_TIMEOUT_SECONDS)
