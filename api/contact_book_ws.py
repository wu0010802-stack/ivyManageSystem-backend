"""api/contact_book_ws.py — 聯絡簿 WebSocket 端點。

雙 channel：
- 教師端：依 classroom_id 訂閱（看到自己班級被家長 ack 的計數即時更新）
- 家長端：依 parent_user_id 訂閱（聯絡簿發布即時通知）

從 ChannelHub 遷移到 utils/broadcast.BroadcastBackend；caller helper
broadcast_classroom / broadcast_parent 簽章保留。
"""

import logging

from fastapi import APIRouter, HTTPException, WebSocket

from api.portal._shared import (
    _get_teacher_classroom_ids as _get_teacher_classroom_ids_shared,
)
from models.database import get_session
from utils.auth import verify_ws_token
from utils.broadcast import get_broadcast
from utils.permissions import Permission, has_permission
from utils.ws_connection_limiter import (
    WSConnectionLimitExceeded,
    assert_under_limit,
    register,
    unregister,
)
from utils.ws_hub import (
    WS_CLOSE_FORBIDDEN,
    WS_CLOSE_INVALID_TOKEN,
    WS_CLOSE_MISSING_TOKEN,
    get_token_from_ws,
    run_ws_connection,
)

logger = logging.getLogger(__name__)


def _classroom_channel(classroom_id: int) -> str:
    return f"contact_book.classroom.{classroom_id}"


def _parent_channel(parent_user_id: int) -> str:
    return f"contact_book.parent.{parent_user_id}"


async def broadcast_classroom(classroom_id: int, event: dict) -> None:
    """供 service 層呼叫：將事件推送至教師班級 channel。"""
    await get_broadcast().publish(_classroom_channel(classroom_id), event)


async def broadcast_parent(parent_user_id: int, event: dict) -> None:
    """供 service 層呼叫：將事件推送至特定家長 channel。"""
    await get_broadcast().publish(_parent_channel(parent_user_id), event)


def _get_teacher_classroom_ids(employee_id: int) -> list[int]:
    session = get_session()
    try:
        return _get_teacher_classroom_ids_shared(session, employee_id)
    finally:
        session.close()


ws_router = APIRouter()


@ws_router.websocket("/api/ws/portal/contact-book")
async def portal_contact_book_ws(ws: WebSocket):
    """教師端聯絡簿 WS — 收到自己班級的 ack/reply 即時通知。"""
    token = get_token_from_ws(ws)
    if not token:
        await ws.close(code=WS_CLOSE_MISSING_TOKEN, reason="未提供 Token")
        return

    try:
        payload = verify_ws_token(token)
    except HTTPException as e:
        code = WS_CLOSE_FORBIDDEN if e.status_code == 403 else WS_CLOSE_INVALID_TOKEN
        await ws.close(code=code, reason=e.detail)
        return
    except Exception:
        await ws.close(code=WS_CLOSE_INVALID_TOKEN, reason="Token 無效或已過期")
        return

    role = payload.get("role", "")
    if role not in ("teacher", "admin", "supervisor", "hr"):
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="此帳號無權限訂閱聯絡簿")
        return

    perms = payload.get("permission_names")
    if not (
        has_permission(perms, Permission.PORTFOLIO_READ)
        or has_permission(perms, Permission.PORTFOLIO_WRITE)
    ):
        await ws.close(
            code=WS_CLOSE_FORBIDDEN, reason="權限不足，需要 portfolio 讀取權限"
        )
        return

    user_id = payload.get("user_id")
    if not user_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="缺少 user_id")
        return

    try:
        assert_under_limit(user_id)
    except WSConnectionLimitExceeded:
        await ws.close(code=1008, reason="ws_connection_limit_exceeded")
        return

    classroom_ids: list[int] = []
    employee_id = payload.get("employee_id")
    if role == "teacher" and employee_id:
        classroom_ids = _get_teacher_classroom_ids(employee_id)

    backend = get_broadcast()
    await ws.accept()
    register(user_id, ws)
    if classroom_ids:
        for cid in classroom_ids:
            backend.subscribe(_classroom_channel(cid), ws)

    def _cleanup():
        backend.unsubscribe(ws)
        unregister(ws)

    await run_ws_connection(ws, cleanup=_cleanup)


@ws_router.websocket("/api/ws/parent/contact-book")
async def parent_contact_book_ws(ws: WebSocket):
    """家長端聯絡簿 WS — 收到自己子女的聯絡簿發布即時通知。"""
    token = get_token_from_ws(ws)
    if not token:
        await ws.close(code=WS_CLOSE_MISSING_TOKEN, reason="未提供 Token")
        return

    try:
        payload = verify_ws_token(token)
    except HTTPException as e:
        code = WS_CLOSE_FORBIDDEN if e.status_code == 403 else WS_CLOSE_INVALID_TOKEN
        await ws.close(code=code, reason=e.detail)
        return
    except Exception:
        await ws.close(code=WS_CLOSE_INVALID_TOKEN, reason="Token 無效或已過期")
        return

    if payload.get("role") != "parent":
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="此 WS 僅家長端可用")
        return

    user_id = payload.get("user_id")
    if not user_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="缺少 user_id")
        return

    try:
        assert_under_limit(user_id)
    except WSConnectionLimitExceeded:
        await ws.close(code=1008, reason="ws_connection_limit_exceeded")
        return

    backend = get_broadcast()
    await ws.accept()
    register(user_id, ws)
    backend.subscribe(_parent_channel(user_id), ws)

    def _cleanup():
        backend.unsubscribe(ws)
        unregister(ws)

    await run_ws_connection(ws, cleanup=_cleanup)
