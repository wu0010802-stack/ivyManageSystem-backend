"""
LINE Messaging API 通知服務
"""

import logging
from datetime import date
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


# ── 訊息建構（純函式，方便測試）──────────────────────────────────────────────

def build_leave_message(
    name: str,
    leave_type: str,
    start: date,
    end: date,
    hours: float,
) -> str:
    """建構請假通知訊息文字"""
    if start == end:
        date_str = start.isoformat()
    else:
        date_str = f"{start.isoformat()} ～ {end.isoformat()}"
    return (
        f"【請假申請】\n"
        f"員工：{name}\n"
        f"假別：{leave_type}\n"
        f"日期：{date_str}\n"
        f"時數：{hours}h\n"
        f"狀態：待主管核准"
    )


def build_overtime_message(
    name: str,
    ot_date: date,
    ot_type: str,
    hours: float,
    use_comp: bool,
) -> str:
    """建構加班通知訊息文字"""
    tag = "補休申請" if use_comp else "加班申請"
    return (
        f"【{tag}】\n"
        f"員工：{name}\n"
        f"類型：{ot_type}\n"
        f"日期：{ot_date.isoformat()}\n"
        f"時數：{hours}h\n"
        f"狀態：待主管核准"
    )


# ── Service ──────────────────────────────────────────────────────────────────

class LineService:
    """LINE 通知 Singleton 服務，支援熱更新設定"""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._target_id: Optional[str] = None
        self._enabled: bool = False

    def configure(self, token: str, target_id: str, enabled: bool) -> None:
        """熱更新設定（不需重啟服務）"""
        self._token = token
        self._target_id = target_id
        self._enabled = enabled

    def _push(self, text: str) -> bool:
        """推送純文字訊息到 LINE，成功回傳 True，失敗回傳 False"""
        if not self._enabled or not self._token or not self._target_id:
            return False
        try:
            resp = requests.post(
                _LINE_PUSH_URL,
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "to": self._target_id,
                    "messages": [{"type": "text", "text": text}],
                },
                timeout=5,
            )
            if resp.status_code != 200:
                logger.warning("LINE API 回傳非 200: %s %s", resp.status_code, resp.text)
                return False
            return True
        except Exception as exc:
            logger.warning("LINE 推送失敗: %s", exc)
            return False

    def notify_leave_submitted(
        self,
        name: str,
        leave_type: str,
        start: date,
        end: date,
        hours: float,
    ) -> None:
        """請假申請送出後通知（失敗時 log warning，不拋出）"""
        text = build_leave_message(name, leave_type, start, end, hours)
        self._push(text)

    def notify_overtime_submitted(
        self,
        name: str,
        ot_date: date,
        ot_type: str,
        hours: float,
        use_comp: bool,
    ) -> None:
        """加班申請送出後通知（失敗時 log warning，不拋出）"""
        text = build_overtime_message(name, ot_date, ot_type, hours, use_comp)
        self._push(text)
