"""utils/exceptions.py — 業務例外型別。

BusinessError 用於表達「已分類的業務錯誤」，由全域 handler（utils/exception_handlers）
統一包成 envelope `{detail: {code, message, request_id[, ...extra]}}` 回應。

何時用：
- inline 條件判斷後想拋 4xx 並帶結構化 `code` 給前端切 i18n/UX 用
- 既有 raise HTTPException(400, "中文字串") 想升級為帶 code 的版本

何時不用：
- 純 FastAPI dependency / Pydantic 驗證錯誤 → 走 422 handler
- 未捕捉到的程式 bug → 走 unhandled handler
"""

from typing import Any


class BusinessError(Exception):
    """業務錯誤：含 error code、訊息、HTTP status 與可選 extra dict。"""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 400,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not code:
            raise ValueError("BusinessError code 不可為空字串")
        if not message:
            raise ValueError("BusinessError message 不可為空字串")
        self.code = code
        self.message = message
        self.http_status = http_status
        self.extra = extra
        super().__init__(message)

    def __repr__(self) -> str:
        return f"BusinessError(code={self.code!r}, http_status={self.http_status})"
