"""Request body 大小上限 middleware（防超大 body 撐爆單 worker 記憶體）。

Starlette/uvicorn 預設不限 request body 大小。單一超大 body（數百 MB JSON）會在進
handler / Pydantic 驗證前被收進記憶體 → 單 uvicorn worker 記憶體飆升、影響全站。

兩道防線（純 ASGI，不額外緩衝整個 body）：
1. Content-Length 預檢：宣告大小超限 → 先於讀 body 回乾淨 413。涵蓋多數客戶端與
   「宣告大小的緩衝攻擊」。
2. Streaming 累計：對 chunked / 無 Content-Length / Content-Length 偽報過小者，逐塊
   累計位元組；超限即回 http.disconnect 截斷 body（下游讀到不完整 body → 400/422），
   記憶體上限收斂在 max_body_bytes。

上限見 config.network.max_request_body_bytes（預設 64MB，高於最大合法上傳 50MB）。
"""

from __future__ import annotations

import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# 413 JSON body（中文「請求內容過大」以 \u 轉義避免原始碼非 ASCII bytes 編碼問題）
_TOO_LARGE_BODY = (
    b'{"detail":"\\u8acb\\u6c42\\u5167\\u5bb9\\u904e\\u5927\\uff0c'
    b'\\u8acb\\u7e2e\\u5c0f\\u5f8c\\u91cd\\u8a66"}'
)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1) Content-Length 預檢（先於讀 body）
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    if int(value) > self.max_body_bytes:
                        logger.warning(
                            "request body Content-Length=%s 超過上限 %d，回 413 path=%s",
                            value.decode("latin-1", "replace"),
                            self.max_body_bytes,
                            scope.get("path"),
                        )
                        await self._reject(send)
                        return
                except ValueError:
                    pass
                break

        # 2) Streaming 累計（chunked / 無 Content-Length / 偽報過小）
        total = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal total, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    exceeded = True
                    logger.warning(
                        "request body streaming 超過上限 %d，截斷 body path=%s",
                        self.max_body_bytes,
                        scope.get("path"),
                    )
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send)

    async def _reject(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(_TOO_LARGE_BODY)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _TOO_LARGE_BODY})
