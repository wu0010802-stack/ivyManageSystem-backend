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
from utils.portfolio_access import is_unrestricted
from utils.ws_connection_limiter import (
    WSConnectionLimitExceeded,
    assert_under_limit,
    register,
    unregister,
)
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
    """教師端接送通知 WebSocket：推送接送通知事件。

    授權對齊同資源的 REST 端點 `/api/portal/dismissal-calls`
    （require_permission(DISMISSAL_CALLS_READ)）——gate 在權限而非硬卡 role=="teacher"。
    早期版本的 teacher-only 守衛比 REST 嚴格，導致 admin（持有此權限、直接檢視教師端）
    REST 拿得到資料卻被 WS handshake 拒於門外（pre-accept close → 瀏覽器看到 1006），
    前端重試耗盡退化成 polling，呈現「一打開就連不上」。

    訂閱範圍同樣對齊 REST 的 is_unrestricted scope：
      - :all（管理/資深角色，或 wildcard）→ 訂閱 admin channel 收全校事件
      - :own_class（一般教師）→ 訂閱自己班級 channel
    認證透過 httpOnly Cookie access_token（瀏覽器自動攜帶；園長預覽走 impersonate 後
    cookie 已是 teacher token，與一般教師同路徑）。
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

    # 授權：與 REST 端點同一條 require_permission(DISMISSAL_CALLS_READ)。
    # 不再硬卡 role=="teacher"——比 REST 嚴格的 WS 守衛正是原 bug 來源。
    if not has_permission(
        payload.get("permission_names"), Permission.DISMISSAL_CALLS_READ
    ):
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="權限不足，需要接送通知讀取權限")
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

    # 訂閱範圍對齊 REST scope（is_unrestricted 同一呼叫，保證 WS 收到的與 REST 回傳一致）。
    backend = get_broadcast()
    if is_unrestricted(payload, code=Permission.DISMISSAL_CALLS_READ.value):
        channels = [_ADMIN_CHANNEL]
    else:
        employee_id = payload.get("employee_id")
        channels = (
            [_classroom_channel(cid) for cid in _get_teacher_classroom_ids(employee_id)]
            if employee_id
            else []
        )

    await ws.accept()
    register(user_id, ws)
    for ch in channels:
        backend.subscribe(ch, ws)
    logger.info(
        "Portal 接送通知 WS 已連線（role=%s, channels=%s）",
        payload.get("role", ""),
        channels,
    )

    def _cleanup():
        backend.unsubscribe(ws)
        unregister(ws)

    await _run_connection(ws, cleanup=_cleanup)


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

    # scope 對齊 REST 全園列表 `/api/dismissal-calls`（assert_all_scope(STUDENTS_READ)）：
    # admin channel 收全校事件，須持 :all scope（wildcard / bare STUDENTS_READ / *:all）。
    # 不可只用 bare has_permission——STUDENTS_READ 是 scope-aware，bare 檢查對
    # 'STUDENTS_READ:own_class' 仍回 True，會讓被限自班的管理角色從 WS 收全校 PII
    # （REST 同情境已 403）。對齊同檔 portal WS 的 is_unrestricted scope 守衛。
    if not is_unrestricted(payload, code=Permission.STUDENTS_READ.value):
        await ws.close(
            code=WS_CLOSE_FORBIDDEN,
            reason="權限不足，需要全園學生讀取權限（STUDENTS_READ:all）",
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

    backend = get_broadcast()
    await ws.accept()
    register(user_id, ws)
    backend.subscribe(_ADMIN_CHANNEL, ws)
    logger.info("管理端 WS 已連線")

    def _cleanup():
        backend.unsubscribe(ws)
        unregister(ws)

    await _run_connection(ws, cleanup=_cleanup)
