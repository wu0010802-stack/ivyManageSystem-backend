"""廠商付款簽收 router (api/vendor_payments.py) 對應 Out schemas — Phase 3.5。

涵蓋 grandfather endpoint（全 admin 後台，無公開）：

- GET    /vendor-payments                               → VendorPaymentListOut
- GET    /vendor-payments/{payment_id}                  → VendorPaymentOut
- POST   /vendor-payments/{payment_id}/attachments      → VendorPaymentAttachmentMetaOut

Out of scope (defer)：
- GET /vendor-payments/{payment_id}/signature              → FileResponse (二進位圖檔)
- GET /vendor-payments/{payment_id}/attachments/download   → FileResponse (附件下載)

PII 註解：
- ``vendor_name`` 為廠商名稱（業務必看，非個人 PII，但 substring 命中 ``vendor``
  保留性質標 pii-allow）。
- ``amount`` 為付款金額（廠商付款明細，行政人員必看；substring 命中 ``amount``
  保留性質標 pii-allow）。
- ``invoice_number`` 為發票/收據號碼（行政帳務需看）。
- ``description`` / ``notes`` 為自填備註（可能含廠商聯絡或品項細節，本人可見）。
- ``signer_name`` / ``created_by_name`` 為內部員工姓名（員工自家後台 UI 必顯示，
  非跨人 PII），同 Sentry exempt 機制。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel
from schemas._common import (  # noqa: F401  (re-export 便於 router import)
    DeleteResultOut,
    MutationResultOut,
)

# ============ Shared sub-schemas ============


class VendorPaymentAttachmentMetaOut(IvyBaseModel):
    """廠商付款附件 metadata（單筆）。

    對應 ``VendorPayment.attachments`` JSONB list item，與
    ``POST /vendor-payments/{id}/attachments`` 回傳同 shape。
    """

    key: str
    filename: str  # pii-allow: 原始上傳檔名（行政帳務必看）
    size: int
    mime_type: Optional[str] = None
    uploaded_at: Optional[str] = None
    uploaded_by_id: Optional[int] = None


# ============ GET /vendor-payments/{payment_id} ============


class VendorPaymentOut(IvyBaseModel):
    """單筆廠商付款（含簽收狀態 / 附件 metadata）。

    對應 router ``_to_dict(row)`` 輸出。
    """

    id: int
    payment_date: Optional[str] = None
    vendor_name: str  # pii-allow: 廠商名稱（行政帳務必看，非個人 PII）
    amount: Optional[float] = (
        None  # pii-allow: 廠商付款金額（業務需看，非員工薪資 PII）
    )
    payment_method: str
    description: Optional[str] = None  # pii-allow: 行政自填說明
    invoice_number: Optional[str] = None  # pii-allow: 發票/收據號碼（帳務必看）
    notes: Optional[str] = None  # pii-allow: 行政自填備註
    attachments: list[VendorPaymentAttachmentMetaOut]
    status: str
    signer_id: Optional[int] = None
    signer_name: Optional[str] = None  # pii-allow: 內部員工姓名（自家後台必顯示）
    signed_at: Optional[str] = None
    signature_kind: Optional[str] = None
    has_signature: bool
    created_by_id: Optional[int] = None
    created_by_name: Optional[str] = None  # pii-allow: 內部員工姓名（自家後台必顯示）
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ============ GET /vendor-payments ============


class VendorPaymentListOut(IvyBaseModel):
    """GET /vendor-payments 分頁列表回傳。"""

    items: list[VendorPaymentOut]
    total: int
    page: int
    page_size: int
