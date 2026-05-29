"""家長端用藥單 (api/parent_portal/medications.py) 對應 Out schemas。

Phase 3.5 範圍（本檔，全 5/5）：
- GET    /parent/medication-orders                              → ParentMedicationOrderListOut
- GET    /parent/medication-orders/{id}                         → ParentMedicationOrderOut
- POST   /parent/medication-orders                              → ParentMedicationOrderOut
- POST   /parent/medication-orders/{id}/photos                  → ParentMedicationPhotoOut
- DELETE /parent/medication-orders/{id}/photos/{attachment_id}  → DeleteResultOut (共用)

與教師端 schemas/student_health.py 命名區隔：
- 教師端 prefix: Medication（含 administered_by / correction_of / updated_at 等 staff metadata）
- 家長端 prefix: ParentMedication（家長視角刪 administered_by / correction_of /
  updated_at；attachment 也只給 url / display_url / thumb_url，不暴露 storage_key）

PII / 健康資料：用藥單本質為學童醫療資訊（醫療法 §67 / 個資法第六條特種個資），
家長為法定代理人取用本人/子女資料屬合法授權，inline `# pii-allow:` 與
schemas/student_health.py 慣例對齊。
"""

from __future__ import annotations

from typing import Literal, Optional

from schemas._base import IvyBaseModel
from schemas._common import DeleteResultOut as DeleteResultOut  # re-export 方便 router

# ──────────────────────────────────────────────────────────────────────
# Building blocks（list / detail / create 共用）
# ──────────────────────────────────────────────────────────────────────


class ParentMedicationLogOut(IvyBaseModel):
    """用藥執行紀錄單筆（家長視角，_log_to_dict 序列化結果）。

    與教師端 MedicationLogOut 區隔：家長端不暴露 administered_by / correction_of /
    created_at（staff audit metadata，家長看不到老師個資）。
    """

    id: int
    order_id: int
    scheduled_time: Optional[str] = None
    status: Literal["pending", "administered", "skipped", "correction"]
    administered_at: Optional[str] = None
    skipped: bool
    skipped_reason: Optional[str] = (
        None  # pii-allow: 跳過原因（用藥審計留痕，家長必看）
    )
    note: Optional[str] = None  # pii-allow: 執行備註（醫療資訊）


class ParentMedicationPhotoOut(IvyBaseModel):
    """藥袋 / 處方照單張（家長視角，_attachment_to_dict 序列化結果）。

    回傳於：
    - POST /{id}/photos（上傳成功單筆）
    - 被 ParentMedicationOrderOut.photos 內嵌

    家長端 URL 走 /api/parent/uploads/portfolio/{key}（見 parent_downloads.py），
    `url` 為原檔、`display_url` 為適合 LIFF 顯示的轉檔（HEIC→JPEG）、`thumb_url`
    為縮圖；storage_key 不直接暴露給家長。
    """

    id: int
    original_filename: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    url: Optional[str] = None
    display_url: Optional[str] = None
    thumb_url: Optional[str] = None
    created_at: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
# GET /{id} + POST / + list 內每筆 → ParentMedicationOrderOut
# ──────────────────────────────────────────────────────────────────────


class ParentMedicationOrderOut(IvyBaseModel):
    """家長端用藥單單筆（_order_to_dict 序列化結果）。

    回傳於：
    - GET /{id}（單筆詳情）
    - POST /（建單成功）
    - 被 ParentMedicationOrderListOut.items 內嵌

    與教師端 MedicationOrderOut 區隔：家長端不暴露 updated_at（教師端
    metadata），photos 用 ParentMedicationPhotoOut（家長視角 URL）。
    """

    id: int
    student_id: int
    order_date: str
    medication_name: str  # pii-allow: 藥名（醫療資訊，家長必看）
    dose: str  # pii-allow: 劑量（醫療資訊，家長必看）
    time_slots: list[str]
    note: Optional[str] = None  # pii-allow: 給藥備註（醫療資訊）
    source: Optional[str] = None
    created_by: Optional[int] = None
    logs: list[ParentMedicationLogOut]
    photos: list[ParentMedicationPhotoOut]
    created_at: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
# GET / → ParentMedicationOrderListOut
# ──────────────────────────────────────────────────────────────────────


class ParentMedicationOrderListOut(IvyBaseModel):
    """GET /parent/medication-orders — 學生用藥單列表（含教師建立 + 家長自建）。"""

    items: list[ParentMedicationOrderOut]
    total: int
