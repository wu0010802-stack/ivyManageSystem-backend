"""magic-link download token 從 access log 遮罩（R6-9）。

GET /api/offboarding/download?token=... 的明文 token 會被 uvicorn 預設 access log
記錄（含 query string）→ log 洩漏即可在 30 天 / 3 次內重用。此純 ASGI middleware 在
請求進入時 **in-place** 將 query_string 的 token 值遮罩成 __redacted__（uvicorn 讀
同一個 scope 物件，故 access log 看到的是遮罩版），原始 token 存進
scope["magic_link_token"] 供 download 端點讀取。
"""

from urllib.parse import parse_qsl, urlencode

_DOWNLOAD_PATH = "/api/offboarding/download"
_REDACTED = "__redacted__"


class MagicLinkLogScrubMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == _DOWNLOAD_PATH:
            qs = scope.get("query_string", b"")
            if b"token=" in qs:
                token = None
                masked = []
                for k, v in parse_qsl(qs.decode("latin-1"), keep_blank_values=True):
                    if k == "token":
                        token = v
                        masked.append((k, _REDACTED))
                    else:
                        masked.append((k, v))
                if token:
                    # in-place 改寫（uvicorn 與 app 共用同一 scope 物件）
                    scope["magic_link_token"] = token
                    scope["query_string"] = urlencode(masked).encode("latin-1")
        await self.app(scope, receive, send)
