"""UptimeRobot webhook receiver — push alert 到 OPS_ALERT_LINE_GROUP_ID。

UptimeRobot 設定 alert contact 為此 endpoint URL，token 用 query param 驗證
（UptimeRobot 不支援 custom header in free tier）。

LineService 由 main.py 透過 init_uptime_webhook_service() 注入（同 ops_alert
pattern）。未注入時自動 no-op（log warn）。

env：
- UPTIME_ROBOT_WEBHOOK_TOKEN — 必填，與 UptimeRobot webhook URL 的 ?token=... 比對
- OPS_ALERT_LINE_GROUP_ID    — 缺即略過 push（log warn + 回 status=skipped）
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

from config import settings

if TYPE_CHECKING:
    from services.line_service import LineService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/internal", tags=["internal"])

_line_service: "LineService | None" = None


def init_uptime_webhook_service(line_service: "LineService") -> None:
    """由 main.py startup 注入單例 LineService。"""
    global _line_service
    _line_service = line_service


def reset_for_tests() -> None:
    """測試 helper：清空注入的 LineService。"""
    global _line_service
    _line_service = None


def _build_message(monitor: str, alert_type: str, details: str) -> str:
    """組中文告警訊息。

    UptimeRobot alertType："1" = down, "2" = up, 其他視為一般更新。
    """
    if alert_type == "1":
        return f"⚠️ 監控告警：{monitor} 宕機\n細節：{details}"
    elif alert_type == "2":
        return f"✅ 監控恢復：{monitor} 已上線"
    else:
        return f"ℹ️ 監控更新：{monitor}\n細節：{details}"


@router.post("/uptime-webhook")
async def uptime_webhook(token: str, request: Request):
    """收 UptimeRobot 告警 → 推 LINE 群。

    token 用 query param（free tier UptimeRobot 不支援 custom header）。
    payload 為 UptimeRobot 標準 JSON：{monitorFriendlyName, alertType, alertDetails}。
    """
    expected_token = settings.ops_alert.uptime_robot_webhook_token
    if not expected_token or token != expected_token:
        raise HTTPException(status_code=401, detail="invalid token")

    payload = await request.json()
    monitor = payload.get("monitorFriendlyName", "<unknown>")
    alert_type = str(payload.get("alertType", ""))
    details = payload.get("alertDetails", "")

    message = _build_message(monitor, alert_type, details)

    group_id = settings.ops_alert.line_group_id
    if not group_id:
        logger.warning(
            "OPS_ALERT_LINE_GROUP_ID 未設定，UptimeRobot alert 跳過 LINE push (monitor=%s, type=%s)",
            monitor,
            alert_type,
        )
        return {"status": "skipped"}

    if _line_service is None:
        logger.warning(
            "LineService 未注入（init_uptime_webhook_service 未呼叫）；"
            "UptimeRobot alert monitor=%s 跳過 LINE push",
            monitor,
        )
        return {"status": "skipped"}

    try:
        _line_service.push_text_to_group(group_id, message)
    except Exception as e:  # noqa: BLE001
        logger.error(
            "UptimeRobot alert LINE push 失敗 (monitor=%s): %s",
            monitor,
            e,
            exc_info=True,
        )
        return {"status": "push_failed"}
    return {"status": "ok"}
