"""教師端聯絡簿 (api/portal/contact_book.py) 對應 Out schemas。

Phase 3.5 範圍（本檔）：
- GET    /portal/contact-book                       → ContactBookListOut
- POST   /portal/contact-book/batch                 → ContactBookBatchUpsertOut
- PUT    /portal/contact-book/{id}                  → ContactBookEntryOut
- POST   /portal/contact-book/{id}/publish          → ContactBookEntryOut
- POST   /portal/contact-book/{id}/photos           → ContactBookPhotoOut
- GET    /portal/contact-book/unpublished           → ContactBookUnpublishedOut
- POST   /portal/contact-book/copy-from-yesterday   → ContactBookCopyYesterdayOut
- POST   /portal/contact-book/apply-template        → ContactBookApplyTemplateOut
- POST   /portal/contact-book/batch-publish         → ContactBookBatchPublishOut
- DELETE /portal/contact-book/{id}/photos/{att_id}  → DeleteResultOut (共用)

聯絡簿欄位（mood / teacher_note / 學童姓名）標 pii-allow：教師端必看，
並與 Sentry denylist exempt 對齊（同 portal_students.py 慣例）。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel

# ──────────────────────────────────────────────────────────────────────
# Photo / Entry building blocks（多 endpoint 共用）
# ──────────────────────────────────────────────────────────────────────


class ContactBookPhotoOut(IvyBaseModel):
    """單張照片回傳。POST /{id}/photos 的 response，並用於 entry.photos 內。"""

    id: int
    display_url: Optional[str] = None
    thumb_url: Optional[str] = None
    original_filename: Optional[str] = None


class ContactBookEntryOut(IvyBaseModel):
    """聯絡簿單筆 entry payload（_entry_to_dict 序列化結果）。

    回傳於：
    - PUT /portal/contact-book/{id}（單筆編輯）
    - POST /portal/contact-book/{id}/publish（發布）
    - 被 ContactBookListItem.entry 內嵌
    """

    id: int
    student_id: int
    classroom_id: int
    log_date: Optional[str] = None
    mood: Optional[str] = None  # pii-allow: 學童情緒（教師端必看）
    meal_lunch: Optional[int] = None
    meal_snack: Optional[int] = None
    nap_minutes: Optional[int] = None
    bowel: Optional[str] = None
    temperature_c: Optional[float] = None  # pii-allow: 學童體溫
    teacher_note: Optional[str] = None  # pii-allow: 教師備註（含學童觀察）
    learning_highlight: Optional[str] = None  # pii-allow: 學習亮點
    published_at: Optional[str] = None
    version: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    photos: list[ContactBookPhotoOut] = []


# ──────────────────────────────────────────────────────────────────────
# GET / → ContactBookListOut
# ──────────────────────────────────────────────────────────────────────


class ContactBookCompletion(IvyBaseModel):
    """班級當日聯絡簿完成度（compute_class_completion 回傳）。"""

    roster: int
    draft: int
    published: int
    missing: int


class ContactBookListItem(IvyBaseModel):
    """list_classroom_day 內每位學生一筆。"""

    student_id: int
    student_name: str  # pii-allow: 學童姓名（教師端必看）
    entry: Optional[ContactBookEntryOut] = None


class ContactBookListOut(IvyBaseModel):
    """GET /portal/contact-book — 某班某日聯絡簿列表。"""

    classroom_id: int
    log_date: str
    completion: ContactBookCompletion
    items: list[ContactBookListItem]


# ──────────────────────────────────────────────────────────────────────
# POST /batch → ContactBookBatchUpsertOut
# ──────────────────────────────────────────────────────────────────────


class ContactBookBatchUpsertOut(IvyBaseModel):
    """POST /portal/contact-book/batch — 班級表式批次 upsert 回傳。"""

    classroom_id: int
    log_date: str
    entry_ids: list[int]


# ──────────────────────────────────────────────────────────────────────
# GET /unpublished → ContactBookUnpublishedOut
# ──────────────────────────────────────────────────────────────────────


class ContactBookUnpublishedItem(IvyBaseModel):
    """list_unpublished 內單筆草稿（精簡欄位 + 學生姓名）。"""

    id: int
    student_id: int
    student_name: Optional[str] = None  # pii-allow: 學童姓名（教師端必看）
    version: int
    updated_at: Optional[str] = None


class ContactBookUnpublishedOut(IvyBaseModel):
    """GET /portal/contact-book/unpublished — 草稿列表（便於批次發布）。"""

    classroom_id: int
    log_date: str
    items: list[ContactBookUnpublishedItem]


# ──────────────────────────────────────────────────────────────────────
# POST /copy-from-yesterday → ContactBookCopyYesterdayOut
# ──────────────────────────────────────────────────────────────────────


class ContactBookCopyYesterdayOut(IvyBaseModel):
    """POST /portal/contact-book/copy-from-yesterday — 複製昨日草稿結果。"""

    classroom_id: int
    target_date: str
    created: int


# ──────────────────────────────────────────────────────────────────────
# POST /apply-template → ContactBookApplyTemplateOut
# ──────────────────────────────────────────────────────────────────────


class ContactBookApplyTemplateItem(IvyBaseModel):
    """apply_template 單筆套用結果。"""

    entry_id: int
    changed_fields: list[str]
    version: int


class ContactBookApplyTemplateOut(IvyBaseModel):
    """POST /portal/contact-book/apply-template — 範本套用結果列表。"""

    template_id: int
    results: list[ContactBookApplyTemplateItem]


# ──────────────────────────────────────────────────────────────────────
# POST /batch-publish → ContactBookBatchPublishOut
# ──────────────────────────────────────────────────────────────────────


class ContactBookBatchPublishItem(IvyBaseModel):
    """batch_publish 單筆結果（status="ok" 或 "error" + message）。"""

    entry_id: int
    status: str
    message: Optional[str] = None


class ContactBookBatchPublishOut(IvyBaseModel):
    """POST /portal/contact-book/batch-publish — 批次發布結果。"""

    results: list[ContactBookBatchPublishItem]
    success_count: int
