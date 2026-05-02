"""
api/contact_book_ws.py — 聯絡簿 WebSocket 端點

雙 channel：
- 教師端：依 classroom_id 訂閱（可看到自己班級被家長 ack 的計數即時更新）
- 家長端：依 parent_user_id 訂閱（聯絡簿發布即時通知）

複用 utils/ws_hub.ChannelHub + run_ws_connection。
"""

import logging

from fastapi import APIRouter, HTTPException, WebSocket

from api.portal._shared import (
    _get_teacher_classroom_ids as _get_teacher_classroom_ids_shared,
)
from models.database import get_session
from utils.auth import verify_ws_token
from utils.permissions import Permission
from utils.ws_hub import (
    WS_CLOSE_FORBIDDEN,
    WS_CLOSE_INVALID_TOKEN,
    WS_CLOSE_MISSING_TOKEN,
    ChannelHub,
    get_token_from_ws,
    run_ws_connection,
)

logger = logging.getLogger(__name__)

# 全域 hub singleton（供 service 層 broadcast 用）
hub = ChannelHub()

# Channel key 慣例：
#   ("classroom", classroom_id) — 教師端 / 管理端 訂閱班級事件
#   ("parent", parent_user_id)  — 家長端 訂閱自己子女的聯絡簿事件
TEACHER_CLASSROOM_KEY = lambda cid: ("classroom", cid)
PARENT_USER_KEY = lambda uid: ("parent", uid)


async def broadcast_classroom(classroom_id: int, event: dict) -> None:
    """供 service 層呼叫：將事件推送至教師班級 channel。"""
    await hub.broadcast([TEACHER_CLASSROOM_KEY(classroom_id)], event)


async def broadcast_parent(parent_user_id: int, event: dict) -> None:
    """供 service 層呼叫：將事件推送至特定家長 channel。"""
    await hub.broadcast([PARENT_USER_KEY(parent_user_id)], event)


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

    permissions = payload.get("permissions", 0)
    if not (
        permissions == -1
        or (permissions & Permission.PORTFOLIO_READ.value)
        or (permissions & Permission.PORTFOLIO_WRITE.value)
    ):
        await ws.close(
            code=WS_CLOSE_FORBIDDEN, reason="權限不足，需要 portfolio 讀取權限"
        )
        return

    classroom_ids: list[int] = []
    employee_id = payload.get("employee_id")
    if role == "teacher" and employee_id:
        classroom_ids = _get_teacher_classroom_ids(employee_id)

    await ws.accept()
    if classroom_ids:
        for cid in classroom_ids:
            hub.subscribe(TEACHER_CLASSROOM_KEY(cid), ws)
    await run_ws_connection(ws, cleanup=lambda: hub.unsubscribe(ws))


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

    await ws.accept()
    hub.subscribe(PARENT_USER_KEY(user_id), ws)
    await run_ws_connection(ws, cleanup=lambda: hub.unsubscribe(ws))
