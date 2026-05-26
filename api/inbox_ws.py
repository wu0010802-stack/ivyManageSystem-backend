"""員工通知中心 WS — Phase 1 skeleton。

Phase 1 只提供：
- INBOX_USER_KEY: hub subscription key constructor
- inbox_broadcast_user: 推送 helper（無 subscriber 時 no-op）

Phase 3 補完：
- @router.websocket("/inbox") endpoint
- JWT cookie auth → user_id → subscribe
- 重連 / heartbeat
"""

from __future__ import annotations

from typing import Any

from utils.ws_hub import ChannelHub

# 全域 hub singleton（供 service 層 broadcast 用）
hub = ChannelHub()

INBOX_USER_KEY = lambda user_id: ("inbox_user", user_id)


async def inbox_broadcast_user(user_id: int, payload: dict[str, Any]) -> None:
    """推送一筆通知給單一員工的 inbox WS subscriber。

    無 subscriber 時 no-op（hub.broadcast 內部處理）。
    Phase 1 caller 為 services/notification/_channels/ws.py:_inbox_ws_push（同步 wrapper）。
    """
    await hub.broadcast([INBOX_USER_KEY(user_id)], payload)
