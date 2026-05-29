"""教師端聯絡簿範本 (api/portal/contact_book_templates.py) 對應 Out schemas。

Phase 3.5 範圍（本檔）：
- GET    /portal/contact-book/templates                      → ContactBookTemplateListOut
- POST   /portal/contact-book/templates                      → ContactBookTemplateOut
- PATCH  /portal/contact-book/templates/{id}                 → ContactBookTemplateOut
- DELETE /portal/contact-book/templates/{id}                 → DeleteResultOut（共用）
- POST   /portal/contact-book/templates/{id}/promote         → ContactBookTemplateOut

範本檔（template）為教師預先擬好的聯絡簿欄位「預設值」（mood / 餐量 / 午睡 /
教師備註 等），非實際學生紀錄；但 `teacher_note` / `learning_highlight` 為自由
文字，教師可能在 template 內塞含學童觀察的固定句型 — 與 portal_contact_book.py
慣例對齊，標 pii-allow（教師端必看 + Sentry denylist exempt）。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class ContactBookTemplateFieldsOut(IvyBaseModel):
    """範本欄位預設值（與 ContactBookEntryFields 同子集，皆 optional）。

    教師建立範本時可填部分欄位，套用至實際聯絡簿時將這些值帶入。
    """

    mood: Optional[str] = None
    meal_lunch: Optional[int] = None
    meal_snack: Optional[int] = None
    nap_minutes: Optional[int] = None
    bowel: Optional[str] = None
    temperature_c: Optional[float] = None
    teacher_note: Optional[str] = None  # pii-allow: 教師備註預設句型（可含學童觀察）
    learning_highlight: Optional[str] = None  # pii-allow: 學習亮點預設句型


class ContactBookTemplateOut(IvyBaseModel):
    """單筆範本回傳。

    回傳於：
    - POST /portal/contact-book/templates（建立）
    - PATCH /portal/contact-book/templates/{id}（編輯）
    - POST /portal/contact-book/templates/{id}/promote（升級為共用）
    - 被 ContactBookTemplateListOut.items 內嵌
    """

    id: int
    name: str
    scope: str  # "personal" | "shared"
    owner_user_id: Optional[int] = None
    classroom_id: Optional[int] = None
    fields: ContactBookTemplateFieldsOut
    is_archived: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ContactBookTemplateListOut(IvyBaseModel):
    """GET /portal/contact-book/templates — 教師可見範本列表。"""

    items: list[ContactBookTemplateOut]
