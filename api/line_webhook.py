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
        raise HTTPException(
            status_code=503, detail="尚未設定 LINE Channel Secret，無法驗證 Webhook"
        )

    body = await request.body()
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    if not hmac.compare_digest(expected, x_line_signature or ""):
        raise HTTPException(status_code=400, detail="LINE Signature 驗證失敗")

    return body


# ── Webhook endpoint ───────────────────────────────────────────────────────────


@router.post("/webhook")
async def line_webhook(body: bytes = Depends(verify_line_signature)):
    """接收 LINE Platform 傳入的事件，依類型分發處理。

    Phase 5：
    - webhookEventId 去重（LINE retry 防護）
    - 區分教師（既有指令 handler）與家長（reply / postback 雙向）
    - postback 寫 LineReplyContext，後續訊息歸 thread
    """
    import json

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="無效的 JSON")

    for event in payload.get("events", []):
        event_type = event.get("type")
        source = event.get("source", {})
        source_user_id = source.get("userId", "")
        source_group_id = source.get("groupId", "")
        source_room_id = source.get("roomId", "")
        reply_token = event.get("replyToken", "")
        webhook_event_id = event.get("webhookEventId", "")

        if source_group_id or source_room_id:
            logger.info(
                "LINE webhook source: type=%s groupId=%s roomId=%s userId=%s",
                event_type,
                source_group_id or "-",
                source_room_id or "-",
                source_user_id or "-",
            )

        # 去重：webhookEventId UNIQUE 已處理過則跳過
        if _line_service and webhook_event_id:
            from services.line_reply_router import deduplicate_event

            session = get_session()
            try:
                fresh = deduplicate_event(
                    session,
                    webhook_event_id=webhook_event_id,
                    event_type=event_type or "unknown",
                    line_user_id=source_user_id or None,
                )
                session.commit()
            except Exception:
                logger.warning("webhook 去重 insert 失敗（不阻斷）", exc_info=True)
                fresh = True
            finally:
                session.close()
            if not fresh:
                logger.info(
                    "LINE webhook 重複 event 跳過：webhookEventId=%s",
                    webhook_event_id,
                )
                continue

        if event_type == "follow":
            # 用戶加入好友或解除封鎖：
            # 1. 若該 LINE userId 已綁定 User，寫入 line_follow_confirmed_at
            # 2. 仍回覆 User ID 給沒綁的使用者（向下相容既有教師流程）
            if _line_service and source_user_id:
                from datetime import datetime as _dt
                from models.database import User as _User

                session = get_session()
                try:
                    user = (
                        session.query(_User)
                        .filter(_User.line_user_id == source_user_id)
                        .first()
                    )
                    if user is not None:
                        user.line_follow_confirmed_at = _dt.now()
                        session.commit()
                except Exception:
                    logger.warning(
                        "follow event 寫入 line_follow_confirmed_at 失敗（已忽略）",
                        exc_info=True,
                    )
                finally:
                    session.close()

                _line_service._reply(
                    reply_token,
                    f"您的 LINE User ID 是：{source_user_id}\n"
                    "請至 Portal > 個人資料 > LINE 綁定頁面填入，完成後即可使用互動查詢功能。",
                )

        elif event_type == "message":
            msg = event.get("message", {})
            if msg.get("type") == "text" and source_user_id and _line_service:
                text = msg.get("text", "")
                _dispatch_message(source_user_id, text, reply_token)

        elif event_type == "postback":
            postback = event.get("postback", {})
            data = postback.get("data", "")
            if source_user_id and data and _line_service:
                _dispatch_postback(source_user_id, data, reply_token)

        elif event_type == "join":
            # Bot 被邀請進群組或聊天室：回覆該群組/聊天室 ID，
            # 方便管理員複製到「系統設定 > LINE 設定 > Target ID」。
            target_label = "群組" if source_group_id else "聊天室"
            target_id = source_group_id or source_room_id
            if _line_service and target_id and reply_token:
                _line_service._reply(
                    reply_token,
                    f"本{target_label} ID 是：\n{target_id}\n\n"
                    "請複製此 ID 至「系統設定 > LINE 設定 > Target ID」"
                    "並儲存，即可開始接收行政通知（請假/加班/薪資/接送）。",
                )

        else:
            logger.debug("LINE webhook 收到未處理事件類型: %s", event_type)

    return {"status": "ok"}


def _dispatch_message(line_user_id: str, text: str, reply_token: str) -> None:
    """區分教師 vs 家長，分發 message event。"""
    from models.database import User as _User
    from services.line_reply_router import handle_parent_text_message

    session = get_session()
    try:
        user = session.query(_User).filter(_User.line_user_id == line_user_id).first()
        if user is None:
            _line_service._reply(reply_token, "請先至 Portal 完成 LINE 綁定。")
            return
        if user.role == "parent":
            handle_parent_text_message(
                session,
                line_service=_line_service,
                parent_user=user,
                text=text,
                reply_token=reply_token,
            )
        else:
            # 教師既有指令 handler
            _line_service.handle_webhook_message(
                line_user_id, text, reply_token, session
            )
    except Exception:
        logger.warning("LINE message dispatch 失敗", exc_info=True)
    finally:
        session.close()


def _dispatch_postback(line_user_id: str, data: str, reply_token: str) -> None:
    """目前僅家長路徑使用 postback。"""
    from models.database import User as _User
    from services.line_reply_router import handle_parent_postback

    session = get_session()
    try:
        user = session.query(_User).filter(_User.line_user_id == line_user_id).first()
        if user is None or user.role != "parent":
            return
        handle_parent_postback(
            session,
            line_service=_line_service,
            parent_user=user,
            data=data,
            reply_token=reply_token,
        )
    except Exception:
        logger.warning("LINE postback dispatch 失敗", exc_info=True)
    finally:
        session.close()
