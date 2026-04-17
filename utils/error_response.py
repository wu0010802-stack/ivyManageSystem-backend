"""統一錯誤回應格式（供 OpenAPI schema 與型別提示用）。

現有 raise_safe_500 / HTTPException(detail=...) 的回應結構 `{"detail": str}`
已被前端全面依賴（error.response.data.detail），本模組不改變既有格式，
僅提供：
  1. ErrorResponse Pydantic model —— 讓 router 可以在 responses={422: ...} 宣告
  2. 建議性的輔助函式 raise_validation_error / raise_not_found

未來若要增加 error_type / request_id / trace_id 欄位，
只需擴充 ErrorResponse 並更新 exception handler 即可，
不需要逐一修改呼叫端。
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """FastAPI 錯誤回應標準格式。"""

    detail: str = Field(..., description="人類可讀的錯誤訊息")
    error_type: Optional[str] = Field(
        default=None,
        description="錯誤分類（validation / not_found / forbidden 等），供前端判斷顯示策略",
    )
    request_id: Optional[str] = Field(
        default=None,
        description="關聯請求 ID，供追蹤此次錯誤在日誌中的完整 trace",
    )


# 常用 HTTP 錯誤的便捷 raiser（保持與既有 raise_safe_500 相同的慣例）


def raise_not_found(detail: str = "找不到資源") -> None:
    raise HTTPException(status_code=404, detail=detail)


def raise_forbidden(detail: str = "權限不足") -> None:
    raise HTTPException(status_code=403, detail=detail)


def raise_validation(detail: Any) -> None:
    """422 驗證錯誤；detail 可為 str 或結構化列表（沿用 FastAPI 慣例）。"""
    raise HTTPException(status_code=422, detail=detail)


def raise_conflict(detail: str = "資源衝突") -> None:
    raise HTTPException(status_code=409, detail=detail)


__all__ = [
    "ErrorResponse",
    "raise_not_found",
    "raise_forbidden",
    "raise_validation",
    "raise_conflict",
]
