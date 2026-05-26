"""
api/dismissal_ws.py — 接送通知 WebSocket 端點。

從 DismissalConnectionManager 遷移到 utils/broadcast.BroadcastBackend。
保留 `dismissal_manager` deprecated alias 給既有 caller 過渡（DeprecationWarning）。

認證策略：瀏覽器發起 WS 連線時會自動帶上 httpOnly Cookie，
從 ws.cookies 讀取 access_token，不需要 query param。

心跳與廣播重試：共用 utils/ws_hub.run_ws_connection / 廣播參數。
"""

import logging
import warnings

from fastapi import APIRouter, HTTPException, WebSocket

from models.database import get_session
from utils.auth import verify_ws_token
from api.portal._shared import (
    _get_teacher_classroom_ids as _get_teacher_classroom_ids_shared,
)
from utils.broadcast import get_broadcast
from utils.permissions import Permission, has_permission
from utils.ws_hub import (
    MAX_BROADCAST_RETRIES,
    WS_CLOSE_FORBIDDEN,
    WS_CLOSE_INVALID_TOKEN,
    WS_CLOSE_MISSING_TOKEN,
    get_token_from_ws,
    run_ws_connection as _run_connection,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Channel 命名規範
#   dismissal.classroom.{cid}  — 老師 WS 訂閱所屬班級
#   dismissal.admin            — 管理端 WS 訂閱全部
# ---------------------------------------------------------------------------


def _classroom_channel(classroom_id: int) -> str:
    return f"dismissal.classroom.{classroom_id}"


_ADMIN_CHANNEL = "dismissal.admin"


class _DeprecatedDismissalManager:
    """Deprecated shim — 既有 caller 暫留 dismissal_manager.broadcast 簽章。

    新 caller 請直接呼叫 `get_broadcast().publish_many(...)`。
    """

    async def broadcast(self, classroom_id: int, event: dict) -> None:
        warnings.warn(
            "dismissal_manager.broadcast is deprecated; use "
            "get_broadcast().publish_many([dismissal.classroom.{cid}, "
            "dismissal.admin], event) directly",
            DeprecationWarning,
            stacklevel=2,
        )
        await get_broadcast().publish_many(
            [_classroom_channel(classroom_id), _ADMIN_CHANNEL],
            event,
        )


# 向後相容 alias（phase-1/2a rebase 期間避免炸；下下版移除）
manager = _DeprecatedDismissalManager()
dismissal_manager = manager


# ---------------------------------------------------------------------------
# 輔助函式
# ---------------------------------------------------------------------------


# 維持舊 API：tests / 既有呼叫者用 _get_token_from_ws
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

    # NV10：只允許 teacher 角色訂閱接送通知 WebSocket
    role = payload.get("role", "")
    if role != "teacher":
        await ws.close(
            code=WS_CLOSE_FORBIDDEN, reason="僅教師帳號可使用接送通知 WebSocket"
        )
        return

    classroom_ids = _get_teacher_classroom_ids(employee_id)
    backend = get_broadcast()
    if not classroom_ids:
        # 若老師尚未分班，仍允許連線但不會收到任何班級事件
        await ws.accept()
        await _run_connection(ws)
        return

    await ws.accept()
    for cid in classroom_ids:
        backend.subscribe(_classroom_channel(cid), ws)
    logger.info("教師 WS 已連線，班級 IDs: %s", classroom_ids)
    await _run_connection(ws, cleanup=lambda: backend.unsubscribe(ws))


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

    # 白名單檢查
    if payload.get("role") not in ("admin", "hr", "supervisor"):
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="教師帳號不可存取管理端接送通知")
        return

    if not has_permission(payload.get("permission_names"), Permission.STUDENTS_READ):
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="權限不足，需要學生讀取權限")
        return

    backend = get_broadcast()
    await ws.accept()
    backend.subscribe(_ADMIN_CHANNEL, ws)
    logger.info("管理端 WS 已連線")
    await _run_connection(ws, cleanup=lambda: backend.unsubscribe(ws))
