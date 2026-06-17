"""WS 連線數上限 helper — per-user across all WS endpoints。

Instance-local（per-process dict）。Multi-instance 部署時每 instance 各維護
8 條上限，total = instance × 8。WS 連線本來就 instance-sticky（reverse proxy
hash），這個 trade-off 可接受。

威脅：單一 authenticated user 開無限 WS 耗 worker fd / memory → 503 全站。
"""

from __future__ import annotations

import logging
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Hard-code 8 條/user。YAGNI env override；prod 真有反饋再加 config。
WS_MAX_CONN_PER_USER = 8

# user_id → list[WebSocket]
_active_ws: dict[int, list[WebSocket]] = defaultdict(list)


class WSConnectionLimitExceeded(Exception):
    """user 已達 WS 連線上限。caller 應 close ws (code=1008)。"""


def register(user_id: int, ws: WebSocket) -> None:
    """原子 check-and-register：檢查上限並佔用名額，達上限即 raise。

    為避免「判斷上限」與「佔用名額」之間出現 ``await`` yield point
    （並發 handshake 全穿過上限），檢查與 append **同步**在此完成，
    中間無 await。caller 應**先**呼叫本函式佔位、**再** ``await ws.accept()``；
    若 accept 失敗則 unregister 回收（見各 WS 端點）。

    達上限時 raise WSConnectionLimitExceeded 且**不** append。
    """
    if count(user_id) >= WS_MAX_CONN_PER_USER:
        logger.warning(
            "ws_connection_limit_exceeded user_id=%s current=%d max=%d",
            user_id,
            count(user_id),
            WS_MAX_CONN_PER_USER,
        )
        raise WSConnectionLimitExceeded()
    _active_ws[user_id].append(ws)


def unregister(ws: WebSocket) -> None:
    """連線結束 cleanup。idempotent（找不到 user_id 即 noop）。

    caller 應在 finally / run_ws_connection 的 cleanup 接這個。
    """
    for user_id, ws_list in list(_active_ws.items()):
        if ws in ws_list:
            ws_list.remove(ws)
            if not ws_list:
                _active_ws.pop(user_id, None)
            return


def count(user_id: int) -> int:
    """目前該 user 的 active WS 數。"""
    return len(_active_ws.get(user_id, []))


def assert_under_limit(user_id: int) -> None:
    """advisory 預檢：該 user 未超上限；超則 raise WSConnectionLimitExceeded。

    真正具原子性的上限守衛在 register()（check-and-register 無中間 await）。
    本函式保留供呼叫端做早期 close、避免無謂的 accept handshake。
    """
    current = count(user_id)
    if current >= WS_MAX_CONN_PER_USER:
        logger.warning(
            "ws_connection_limit_exceeded user_id=%s current=%d max=%d",
            user_id,
            current,
            WS_MAX_CONN_PER_USER,
        )
        raise WSConnectionLimitExceeded()


def all_active_connections() -> list[WebSocket]:
    """目前所有 active WS（跨 user、跨端點）的快照。

    供 graceful shutdown 主動關閉全部連線用；回傳 list（複製）避免呼叫端
    在 close 過程中變動底層 dict。
    """
    out: list[WebSocket] = []
    for ws_list in _active_ws.values():
        out.extend(ws_list)
    return out


def reset_for_tests() -> None:
    """清掉 in-memory state；只用於 tests / dev 重啟模擬。"""
    _active_ws.clear()
