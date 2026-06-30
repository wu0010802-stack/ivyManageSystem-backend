"""雜項收款簽收 router (api/misc_receipts.py) 對應 Out schemas。

涵蓋（全 admin 後台，無公開）：
- GET  /misc-receipts                          → MiscReceiptListOut
- GET  /misc-receipts/{receipt_id}             → MiscReceiptOut
- GET  /misc-receipts/summary                  → MiscReceiptSummaryOut
- POST /misc-receipts/{receipt_id}/attachments → MiscReceiptAttachmentMetaOut

PII 註解：payer_name / amount / receipt_number / description / notes /
signer_name / created_by_name 均業務必看（非跨人 PII），substring 命中
denylist 故標 pii-allow（同廠商付款 schema 機制）。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel
from schemas._common import (  # noqa: F401
    DeleteResultOut,
    MutationResultOut,
)


class MiscReceiptAttachmentMetaOut(IvyBaseModel):
    """雜項收款附件 metadata（單筆）。"""

    key: str
    filename: str  # pii-allow: 原始上傳檔名（行政帳務必看）
    size: int
    mime_type: Optional[str] = None
    uploaded_at: Optional[str] = None
    uploaded_by_id: Optional[int] = None


class MiscReceiptOut(IvyBaseModel):
    """單筆雜項收款（含簽收狀態 / 附件 metadata）。對應 router _to_dict(row)。"""

    id: int
    receipt_date: Optional[str] = None
    payer_name: str  # pii-allow: 繳款方名稱（行政帳務必看）
    category: str
    amount: Optional[float] = None  # pii-allow: 收款金額（業務需看）
    payment_method: str
    description: Optional[str] = None  # pii-allow: 行政自填說明
    receipt_number: Optional[str] = None  # pii-allow: 收據/單據號碼
    notes: Optional[str] = None  # pii-allow: 行政自填備註
    attachments: list[MiscReceiptAttachmentMetaOut]
    status: str
    signer_id: Optional[int] = None
    signer_name: Optional[str] = None  # pii-allow: 內部員工姓名（自家後台必顯示）
    signed_at: Optional[str] = None
    signature_kind: Optional[str] = None
    has_signature: bool
    created_by_id: Optional[int] = None
    created_by_name: Optional[str] = None  # pii-allow: 內部員工姓名
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MiscReceiptListOut(IvyBaseModel):
    """GET /misc-receipts 分頁列表回傳。"""

    items: list[MiscReceiptOut]
    total: int
    page: int
    page_size: int


class MiscReceiptSummaryOut(IvyBaseModel):
    """GET /misc-receipts/summary 區間彙總（KPI 卡，跨狀態，含 pending）。"""

    total_count: int
    total_amount: float  # pii-allow: 收款金額彙總
    pending_count: int
    pending_amount: float  # pii-allow
    signed_count: int
    signed_amount: float  # pii-allow
