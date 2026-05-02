"""
api/dismissal_ws.py — 接送通知 WebSocket 端點與 ConnectionManager singleton

認證策略：瀏覽器發起 WS 連線時會自動帶上 httpOnly Cookie，
因此從 ws.cookies 讀取 access_token，不需要 query param。

心跳與廣播重試：共用 utils/ws_hub.run_ws_connection / 廣播參數。
"""

import asyncio
import json
import logging
from collections import defaultdict

from fastapi import APIRouter, HTTPException, WebSocket

from models.database import get_session
from utils.auth import verify_ws_token
from api.portal._shared import (
    _get_teacher_classroom_ids as _get_teacher_classroom_ids_shared,
)
from utils.permissions import Permission
from utils.ws_hub import (
    BROADCAST_RETRY_DELAY,
    MAX_BROADCAST_RETRIES,
    PING_INTERVAL,
    PONG_TIMEOUT,
    WS_CLOSE_FORBIDDEN,
    WS_CLOSE_INVALID_TOKEN,
    WS_CLOSE_MISSING_TOKEN,
    get_token_from_ws,
    run_ws_connection as _run_connection,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ConnectionManager
# ---------------------------------------------------------------------------


class DismissalConnectionManager:
    """管理接送通知的 WebSocket 連線。

    - 老師：依所屬班級分組，只收到自己班級的事件
    - 管理端：訂閱全部教室的事件
    """

    def __init__(self):
        # classroom_id → list[WebSocket]
        self._teacher_conns: dict[int, list[WebSocket]] = defaultdict(list)
        # list[WebSocket]（admin 訂閱全部教室）
        self._admin_conns: list[WebSocket] = []

    async def connect_teacher(self, ws: WebSocket, classroom_ids: list[int]):
        await ws.accept()
        for cid in classroom_ids:
            self._teacher_conns[cid].append(ws)
        logger.info("教師 WS 已連線，班級 IDs: %s", classroom_ids)

    async def connect_admin(self, ws: WebSocket):
        await ws.accept()
        self._admin_conns.append(ws)
        logger.info("管理端 WS 已連線，目前共 %d 條管理連線", len(self._admin_conns))

    def disconnect(self, ws: WebSocket):
        for lst in self._teacher_conns.values():
            if ws in lst:
                lst.remove(ws)
        if ws in self._admin_conns:
            self._admin_conns.remove(ws)

    async def broadcast(self, classroom_id: int, event: dict):
        """廣播給該教室的老師 + 所有管理端。

        每個連線最多重試 MAX_BROADCAST_RETRIES 次（間隔 50ms），
        全部失敗後標記為僵死並移除。
        """
        msg = json.dumps(event, ensure_ascii=False, default=str)
        targets = list(self._teacher_conns.get(classroom_id, [])) + list(
            self._admin_conns
        )
        dead = []
        for ws in targets:
            sent = False
            for attempt in range(1, MAX_BROADCAST_RETRIES + 1):
                try:
                    await ws.send_text(msg)
                    sent = True
                    break
                except Exception as exc:
                    if attempt < MAX_BROADCAST_RETRIES:
                        await asyncio.sleep(BROADCAST_RETRY_DELAY)
                    else:
                        logger.warning(
                            "廣播失敗，標記僵死連線（event=%s, 嘗試次數=%d）: %s",
                            event.get("type", "unknown"),
                            attempt,
                            exc,
                        )
            if not sent:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = DismissalConnectionManager()


# ---------------------------------------------------------------------------
# 輔助函式（_run_connection / _get_token_from_ws 改由 utils.ws_hub 提供）
# ---------------------------------------------------------------------------


# 維持舊 API：tests / 既有呼叫者用 _get_token_from_ws，重新匯出 ws_hub 版本
_get_token_from_ws = get_token_from_ws


def _get_teacher_classroom_ids(employee_id: int) -> list[int]:
    """查詢教師目前所屬的班級 ID 列表（含 art_teacher_id）。"""
    session = get_session()
    try:
        return _get_teacher_classroom_ids_shared(session, employee_id)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# WebSocket 路由
# ---------------------------------------------------------------------------

ws_router = APIRouter()


@ws_router.websocket("/api/ws/portal/dismissal-calls")
async def portal_dismissal_ws(ws: WebSocket):
    """教師 Portal WebSocket：只推送自己班級的接送通知事件。
    認證透過 httpOnly Cookie access_token（瀏覽器自動攜帶）。
    """
    token = _get_token_from_ws(ws)
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

    employee_id = payload.get("employee_id")
    if not employee_id:
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="此帳號無對應教師身分")
        return

    # NV10：只允許 teacher 角色訂閱接送通知 WebSocket，防止司機/行政等帳號存取學生接送隱私
    role = payload.get("role", "")
    if role != "teacher":
        await ws.close(
            code=WS_CLOSE_FORBIDDEN, reason="僅教師帳號可使用接送通知 WebSocket"
        )
        return

    classroom_ids = _get_teacher_classroom_ids(employee_id)
    if not classroom_ids:
        # 若老師尚未分班，仍允許連線但不會收到任何班級事件
        await ws.accept()
        await _run_connection(ws)
        return

    await manager.connect_teacher(ws, classroom_ids)
    await _run_connection(ws, cleanup=lambda: manager.disconnect(ws))


@ws_router.websocket("/api/ws/admin/dismissal-calls")
async def admin_dismissal_ws(ws: WebSocket):
    """管理端 WebSocket：接收全部班級的接送通知事件。
    認證透過 httpOnly Cookie access_token（瀏覽器自動攜帶）。
    """
    token = _get_token_from_ws(ws)
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

    # 白名單檢查：role 欄位缺失或不在允許角色清單時一律拒絕，
    # 避免 payload 缺少 role 欄位（None）時跳過防護。
    if payload.get("role") not in ("admin", "hr", "supervisor"):
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="教師帳號不可存取管理端接送通知")
        return

    permissions = payload.get("permissions", 0)
    if not (permissions == -1 or (permissions & Permission.STUDENTS_READ.value)):
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="權限不足，需要學生讀取權限")
        return

    await manager.connect_admin(ws)
    await _run_connection(ws, cleanup=lambda: manager.disconnect(ws))
