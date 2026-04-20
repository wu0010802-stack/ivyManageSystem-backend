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

_STATIC_HEADERS: list[tuple[str, str]] = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    (
        "Content-Security-Policy",
        "default-src 'self'; "
        # 保留 'unsafe-inline'：Vite build 目前仍會注入 inline bootstrap script；移除 'unsafe-eval' 封鎖 eval() / new Function() 的 XSS 面
        "script-src 'self' 'unsafe-inline' https://maps.googleapis.com https://maps.gstatic.com; "
        # 樣式 'unsafe-inline' 仍需保留：Element Plus、Vue <style scoped> 會產生 inline style
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https://*.googleapis.com https://*.gstatic.com https://*.tile.openstreetmap.org; "
        "connect-src 'self' https://maps.googleapis.com https://*.tile.openstreetmap.org wss: ws:; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "  # 封鎖 Flash / 舊式外掛的 XSS 路徑
        "base-uri 'self'; "  # 防止 <base> tag 注入把相對 URL 指向第三方
        "form-action 'self'",  # 表單只能送回本站，防資料外送
    ),
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
