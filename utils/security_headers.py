"""
utils/security_headers.py — HTTP 安全標頭 Middleware

每個回應自動附加：
  - X-Content-Type-Options: nosniff        防止 MIME 嗅探
  - X-Frame-Options: DENY                  防止 Clickjacking
  - Strict-Transport-Security              HSTS（僅正式環境）
  - Referrer-Policy                        限制 Referer 洩漏
  - Content-Security-Policy                限制資源載入來源
"""

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_is_prod = os.environ.get("ENV", "development").lower() in ("production", "prod")


def _build_csp() -> str:
    """組 CSP header（MEDIUM-2）。

    路徑 A（預設）：Vite build 後 dist/index.html 不含 inline <script>，可移除
        `script-src 'unsafe-inline'`。`'unsafe-eval'` 一併不放，封鎖 eval/new Function。
    路徑 B（fallback）：若部署環境（例如 第三方 CDN 或注入 SDK）導致 inline <script>
        無法避免，可透過 env var `CSP_SCRIPT_HASHES`（空白分隔的 'sha256-XXX' 清單）
        加入授權 hash；該 env 缺失時不啟用 fallback。

    `style-src 'unsafe-inline'` 保留：Element Plus / Vue <style scoped> 會產生 inline
    style，移除成本高且報酬有限，屬於工程取捨。
    """
    script_extras = ""
    hashes = os.environ.get("CSP_SCRIPT_HASHES", "").strip()
    if hashes:
        # 允許空白分隔；單個 hash 必須形如 'sha256-XXX'
        token_list = [h.strip() for h in hashes.split() if h.strip()]
        if token_list:
            script_extras = " " + " ".join(token_list)

    return (
        "default-src 'self'; "
        f"script-src 'self'{script_extras} https://maps.googleapis.com https://maps.gstatic.com; "
        # style 'unsafe-inline' 仍保留：Element Plus / Vue <style scoped> 限制
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https://*.googleapis.com https://*.gstatic.com https://*.tile.openstreetmap.org; "
        "connect-src 'self' https://maps.googleapis.com https://*.tile.openstreetmap.org wss: ws:; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


_STATIC_HEADERS: list[tuple[str, str]] = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Content-Security-Policy", _build_csp()),
]

# HSTS 只在正式環境加（HTTP 環境加了也無害，但避免誤導）
if _is_prod:
    _STATIC_HEADERS.append(
        ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """為所有回應注入 HTTP 安全標頭。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        response: Response = await call_next(request)
        for name, value in _STATIC_HEADERS:
            response.headers[name] = value
        return response
