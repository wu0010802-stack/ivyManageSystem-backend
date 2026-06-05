"""WebSocket Origin 防護 middleware（Cross-Site WebSocket Hijacking / CSWSH）。

Finding J：HTTP 的 CSRF/CORS middleware 都是 BaseHTTPMiddleware，**不處理
websocket scope**；WS handshake 又以 httpOnly cookie token 認證。若不驗 Origin，
惡意站可用受害者 cookie 開 WS（`new WebSocket(...)`）收即時 PII（接送學生姓名 /
聯絡簿 / inbox）。

瀏覽器 WS handshake **一律**帶 Origin header。策略：
  - Origin 存在但不在 cors_origins allowlist → 拒絕 handshake（websocket.close 4403）。
  - Origin 缺失（非瀏覽器 client，非 CSWSH 向量）→ 放行（不破壞合法 server-to-server）。

預設 cookie SameSite=strict 已是第一道防線；本 middleware 為第二道、且不依賴
SameSite（跨域部署設 SameSite=none 時仍有效）。純 ASGI middleware，涵蓋全部
（含未來）WS 端點。
"""

from __future__ import annotations

import logging

from middleware.csrf_origin import _get_allowed_origins

logger = logging.getLogger(__name__)

# 4403：自訂 application close code（4000-4999 區間），語意「Origin 不被允許」。
WS_CLOSE_ORIGIN_FORBIDDEN = 4403


class WSOriginCheckMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "websocket":
            origin = None
            for k, v in scope.get("headers") or []:
                if k == b"origin":
                    origin = v.decode("latin-1")
                    break
            if origin and origin not in _get_allowed_origins():
                logger.warning(
                    "WS CSWSH reject: origin=%s path=%s",
                    origin,
                    scope.get("path"),
                )
                # 在 accept 前 close → handshake 被拒（先收 websocket.connect 再 close）。
                try:
                    await receive()
                except Exception:
                    pass
                await send(
                    {"type": "websocket.close", "code": WS_CLOSE_ORIGIN_FORBIDDEN}
                )
                return
        await self.app(scope, receive, send)
