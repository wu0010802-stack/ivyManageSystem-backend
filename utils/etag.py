"""utils/etag.py — HTTP ETag helpers，給 GET endpoint 命中 304 省 payload。

Why: list endpoint（classrooms / employees 等）payload 動輒 50-200 KB，但內容
變動頻率遠低於使用者切頁頻率。掛 ETag 後，瀏覽器自動帶 If-None-Match，
未變動時回 304（無 body），省下序列化 + 網路傳輸 + 前端解析成本。

設計取捨：
- Strong ETag（MD5 整 payload）而非 Weak（W/ 前綴）。命中精度高、實作簡單。
- Cache-Control: private + no-cache。private 阻擋 proxy 跨用戶誤拿（authenticated
  endpoint payload 可能含 per-user 欄位），no-cache 強制 client 每次 revalidate
  而非盲信 max-age。
- 計算 ETag 是必須先算出 payload 才做的事，所以「省 DB query」不在這層的目標；
  只省「序列化 JSON → 網路傳輸 → 前端解析」。如要省 DB，請改用
  services.report_cache_service。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Request, Response
from fastapi.responses import Response as PlainResponse


def compute_etag(payload: Any) -> str:
    """以 MD5 計算 canonical JSON 的 ETag。回傳已加雙引號的字串。"""
    if isinstance(payload, (bytes, bytearray)):
        digest = hashlib.md5(payload).hexdigest()
    else:
        digest = hashlib.md5(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    return f'"{digest}"'


def etag_response(
    request: Request,
    response: Response,
    payload: Any,
    *,
    private: bool = True,
):
    """為 endpoint 套上 ETag + revalidate 行為。

    使用方式（在 router）：
        @router.get("/things")
        def list_things(request: Request, response: Response, ...):
            result = build_things(...)
            return etag_response(request, response, result)

    Args:
        request: FastAPI Request，用來讀取 If-None-Match
        response: FastAPI Response，用來寫 ETag / Cache-Control header
        payload: 任意可 JSON 序列化資料；同時用來算 ETag 與當回傳值
        private: True 表示 authenticated endpoint（含 per-user 資料），加上
            Cache-Control: private 避免共享 proxy 跨用戶誤拿。

    Returns:
        - 命中 If-None-Match → PlainResponse(304) 不含 body
        - 否則回原 payload，response header 已設好 ETag + Cache-Control
    """
    etag = compute_etag(payload)
    cache_control = "private, no-cache" if private else "no-cache"
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = cache_control
    if request.headers.get("If-None-Match") == etag:
        return PlainResponse(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )
    return payload
