"""跨 router 共用 Out schema。

任何 router 用到 `{"message": str, "id": int}` 等通用 shape 一律 import 這裡，
避免每個 schemas/<router>.py 重複定義。
"""

from __future__ import annotations

from typing import Any, Optional

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


class ImportFailureItem(IvyBaseModel):
    """Excel import 單筆失敗紀錄（caller 自訂 data shape）。"""

    row: Optional[int] = None
    error: str
    data: Optional[dict[str, Any]] = None


class ImportResultOut(IvyBaseModel):
    """Excel 批次匯入回傳共用 shape — {succeeded, failed}。"""

    succeeded: int
    failed: list[ImportFailureItem]


class OkStatusOut(IvyBaseModel):
    """純 {status: "ok"} 共用 shape (家長端常用)。"""

    status: str


class UnreadCountOut(IvyBaseModel):
    """{unread_count: int} 共用 shape (家長端通知 / 訊息常用)。"""

    unread_count: int
