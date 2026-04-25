"""services/line_login_service.py — LINE Login (LIFF) id_token 驗證服務

家長入口採 LIFF 登入：前端取得 LINE id_token 後 POST 至後端 LIFF login
endpoint；後端把 id_token 委派給 LINE 官方 /verify 端點驗證簽名與 aud，
免維 JWKS。

注意（plan H 章）：
- LINE userId 為 Provider-scoped。LINE Login Channel（本服務使用）必須
  與既有 Messaging Bot Channel 掛在同一個 LINE Provider 下，否則
  id_token.sub、Messaging webhook source.userId、push 目標 to 會是三個
  不同的值。
- aud 必校驗 == channel_id，防 cross-channel id_token 重放。
"""

import logging
from typing import Optional

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

LINE_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
_VERIFY_TIMEOUT_SECONDS = 5.0


class LineLoginService:
    """LIFF id_token 驗證 singleton。

    main.py 啟動時建立並透過 init_parent_line_service 注入家長 router。
    """

    def __init__(self, channel_id: Optional[str] = None):
        self.channel_id = (channel_id or "").strip()

    def is_configured(self) -> bool:
        return bool(self.channel_id)

    def verify_id_token(self, id_token: str) -> dict:
        """呼叫 LINE /verify 驗證 id_token。

        Returns: payload dict（包含 sub / aud / name / picture / email 等）
        Raises:
            HTTPException(503): channel_id 未設或 LINE 服務不可達
            HTTPException(401): id_token 驗證失敗 / aud 不符 / sub 缺失
        """
        if not self.is_configured():
            raise HTTPException(
                status_code=503,
                detail="LINE Login 尚未設定（缺 LINE_LOGIN_CHANNEL_ID）",
            )
        if not id_token:
            raise HTTPException(status_code=401, detail="缺少 id_token")
        try:
            response = httpx.post(
                LINE_VERIFY_URL,
                data={"id_token": id_token, "client_id": self.channel_id},
                timeout=_VERIFY_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            logger.warning("LINE verify 連線失敗: %s", exc)
            raise HTTPException(
                status_code=503, detail="LINE 驗證服務暫時無法連線"
            )
        if response.status_code != 200:
            logger.warning(
                "LINE verify 失敗 status=%s body=%s",
                response.status_code,
                response.text[:200],
            )
            raise HTTPException(status_code=401, detail="LINE id_token 驗證失敗")
        try:
            payload = response.json()
        except ValueError:
            raise HTTPException(status_code=401, detail="LINE 回應格式異常")
        if payload.get("aud") != self.channel_id:
            logger.warning(
                "LINE verify aud 不符 expected=%s got=%s",
                self.channel_id,
                payload.get("aud"),
            )
            raise HTTPException(status_code=401, detail="LINE id_token aud 不符")
        sub = payload.get("sub")
        if not sub or not isinstance(sub, str):
            raise HTTPException(status_code=401, detail="LINE id_token 缺少 sub")
        return payload
