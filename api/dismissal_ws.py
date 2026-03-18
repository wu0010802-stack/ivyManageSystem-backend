"""
api/dismissal_ws.py — 接送通知 WebSocket 端點與 ConnectionManager singleton

認證策略：瀏覽器發起 WS 連線時會自動帶上 httpOnly Cookie，
因此從 ws.cookies 讀取 access_token，不需要 query param。

心跳機制：
- 伺服器每 PING_INTERVAL 秒送 {"type":"ping"}
- 超過 PONG_TIMEOUT 秒未收到任何 client 訊息，視為僵死並主動關閉
"""

import asyncio
import contextlib
import json
import logging
from collections import defaultdict

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from models.database import get_session, Classroom
from utils.auth import verify_ws_token
from utils.permissions import Permission

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# WebSocket 自訂關閉碼（4000–4999 為應用程式保留範圍，RFC 6455）
# ---------------------------------------------------------------------------
WS_CLOSE_MISSING_TOKEN  = 4001   # 未提供 Token（未登入）
WS_CLOSE_INVALID_TOKEN  = 4003   # Token 無效、已過期、帳號停用或 token_version 不符
WS_CLOSE_FORBIDDEN      = 4007   # Token 有效但權限不足（含 must_change_password）

# ---------------------------------------------------------------------------
# 心跳與廣播參數
# ---------------------------------------------------------------------------
PING_INTERVAL        = 30    # 秒：伺服器送 ping 的間隔
PONG_TIMEOUT         = 90    # 秒：超過此時間無任何 client 訊息即視為僵死
MAX_BROADCAST_RETRIES = 2    # 廣播失敗後的最大重試次數（含首次）


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
        targets = (
            list(self._teacher_conns.get(classroom_id, []))
            + list(self._admin_conns)
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
                        await asyncio.sleep(0.05)
                    else:
                        logger.warning(
                            "廣播失敗，標記僵死連線（event=%s, 嘗試次數=%d）: %s",
                            event.get("type", "unknown"), attempt, exc,
                        )
            if not sent:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = DismissalConnectionManager()


# ---------------------------------------------------------------------------
# 心跳輔助函式
# ---------------------------------------------------------------------------

async def _run_connection(
    ws: WebSocket,
    cleanup=None,
    *,
    ping_interval: float = PING_INTERVAL,
    pong_timeout: float = PONG_TIMEOUT,
) -> None:
    """執行 WebSocket 主循環（含心跳與逾時偵測）。

    心跳機制：
    - ping_task：每 ping_interval 秒送 {"type":"ping"}；送失敗時靜默退出
    - recv_task：接收 client 訊息（pong 或其他）；
                 超過 pong_timeout 秒無任何訊息則主動關閉

    cleanup 在連線結束後（正常斷線 / 逾時 / ping 失敗）皆會呼叫。
    """

    async def _ping_loop():
        while True:
            await asyncio.sleep(ping_interval)
            try:
                await ws.send_text('{"type":"ping"}')
            except Exception:
                logger.debug("WS ping 失敗，連線可能已斷")
                return

    async def _recv_loop():
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=pong_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "WS 連線 %ds 無回應，主動關閉（PONG_TIMEOUT）",
                    int(pong_timeout),
                )
                with contextlib.suppress(Exception):
                    await ws.close()
                return
            except WebSocketDisconnect:
                return

    ping_task = asyncio.create_task(_ping_loop())
    recv_task = asyncio.create_task(_recv_loop())
    try:
        await asyncio.wait({ping_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (ping_task, recv_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if cleanup:
            cleanup()


# ---------------------------------------------------------------------------
# 輔助函式
# ---------------------------------------------------------------------------

def _get_token_from_ws(ws: WebSocket) -> str | None:
    """從 WebSocket 請求的 Cookie 取得 access_token。
    瀏覽器發起同源 WS 連線時會自動攜帶 httpOnly Cookie。
    """
    return ws.cookies.get("access_token")


def _get_teacher_classroom_ids(employee_id: int) -> list[int]:
    """查詢教師目前所屬的班級 ID 列表（head_teacher 或 assistant_teacher）。"""
    session = get_session()
    try:
        classrooms = session.query(Classroom).filter(
            (Classroom.head_teacher_id == employee_id)
            | (Classroom.assistant_teacher_id == employee_id),
            Classroom.is_active == True,
        ).all()
        return [c.id for c in classrooms]
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

    permissions = payload.get("permissions", 0)
    if not (permissions == -1 or (permissions & Permission.STUDENTS_READ.value)):
        await ws.close(code=WS_CLOSE_FORBIDDEN, reason="權限不足，需要學生讀取權限")
        return

    await manager.connect_admin(ws)
    await _run_connection(ws, cleanup=lambda: manager.disconnect(ws))
