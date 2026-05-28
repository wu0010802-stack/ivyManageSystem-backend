"""跨 router 共用 Out schema。

任何 router 用到 `{"message": str, "id": int}` 等通用 shape 一律 import 這裡，
避免每個 schemas/<router>.py 重複定義。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class MutationResultOut(IvyBaseModel):
    """POST/PUT/DELETE 成功回傳的共用 shape — {message, id}。"""

    message: str
    id: int


class DeleteResultOut(IvyBaseModel):
    """純 message DELETE 回傳 — {message}。"""

    message: str


class BulkOpItemResult(IvyBaseModel):
    """批次操作單筆結果。"""

    id: Optional[int] = None
    ok: bool
    error: Optional[str] = None


class BulkOpResultOut(IvyBaseModel):
    """批次操作回傳 — results + 成功/失敗統計。"""

    results: list[BulkOpItemResult]
    success_count: int
    fail_count: int
