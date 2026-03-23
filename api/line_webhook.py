"""
LINE Webhook endpoint — 接收 LINE Platform 事件並分發處理
"""

import base64
import hashlib
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from models.database import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/line", tags=["line-webhook"])

_line_service = None


def init_webhook_service(line_service) -> None:
    global _line_service
    _line_service = line_service


# ── 簽名驗證 dependency ────────────────────────────────────────────────────────

async def verify_line_signature(
    request: Request,
    x_line_signature: Optional[str] = Header(None),
):
    """驗證 LINE 簽名；若未設定 channel_secret 則以 503 拒絕"""
    if _line_service is None:
        raise HTTPException(status_code=503, detail="LINE 服務未初始化")

    secret = _line_service._channel_secret
    if not secret:
        raise HTTPException(status_code=503, detail="尚未設定 LINE Channel Secret，無法驗證 Webhook")

    body = await request.body()
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    if not hmac.compare_digest(expected, x_line_signature or ""):
        raise HTTPException(status_code=400, detail="LINE Signature 驗證失敗")

    return body


# ── Webhook endpoint ───────────────────────────────────────────────────────────

@router.post("/webhook")
async def line_webhook(body: bytes = Depends(verify_line_signature)):
    """接收 LINE Platform 傳入的事件，依類型分發處理"""
    import json

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="無效的 JSON")

    for event in payload.get("events", []):
        event_type = event.get("type")
        source = event.get("source", {})
        source_user_id = source.get("userId", "")
        reply_token = event.get("replyToken", "")

        if event_type == "follow":
            # 用戶加入好友或解除封鎖：回覆其 User ID，引導至 Portal 綁定
            if _line_service and source_user_id:
                _line_service._reply(
                    reply_token,
                    f"您的 LINE User ID 是：{source_user_id}\n"
                    "請至 Portal > 個人資料 > LINE 綁定頁面填入，完成後即可使用互動查詢功能。",
                )

        elif event_type == "message":
            msg = event.get("message", {})
            if msg.get("type") == "text" and source_user_id:
                text = msg.get("text", "")
                session = get_session()
                try:
                    if _line_service:
                        _line_service.handle_webhook_message(
                            source_user_id, text, reply_token, session
                        )
                finally:
                    session.close()

        else:
            logger.debug("LINE webhook 收到未處理事件類型: %s", event_type)

    return {"status": "ok"}
