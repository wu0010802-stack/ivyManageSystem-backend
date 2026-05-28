"""Students router (api/students.py) 對應 Out schemas。

Phase 2 範圍（本檔）：
- GuardianOut / GuardianListOut（_serialize_guardian shape）
- POST /students/{id}/guardians → GuardianOut
- PATCH /students/guardians/{id} → GuardianOut
- DELETE /students/guardians/{id} → MutationResultOut (re-use)
- GET /students/{id}/guardians → GuardianListOut

POST /students / PUT /students/{id} / DELETE /students/{id} / POST graduate /
POST lifecycle 共用 MutationResultOut (re-use from _common)。

Out of scope (Phase 2.5)：
- GET /students (etag-likely + 巢狀 list)
- GET /students/records (items + total 分頁)
- GET /students/{id} 詳情 + GET /students/{id}/academic-summary
+ /students/{id}/profile (大型 nested)
- POST /students/bulk-transfer (複雜結果)
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class GuardianOut(IvyBaseModel):
    """單筆監護人資料 (對應 _serialize_guardian)。"""

    id: int
    student_id: int
    name: Optional[str] = None  # pii-allow: 監護人姓名
    phone: Optional[str] = None  # pii-allow: 監護人電話
    email: Optional[str] = None  # pii-allow: 監護人 email
    relation: Optional[str] = None
    is_primary: bool
    is_emergency: bool
    can_pickup: bool
    custody_note: Optional[str] = None  # pii-allow: 監護權備註
    sort_order: Optional[int] = None


class GuardianListOut(IvyBaseModel):
    """GET /students/{id}/guardians 回傳 — {items}。"""

    items: list[GuardianOut]
