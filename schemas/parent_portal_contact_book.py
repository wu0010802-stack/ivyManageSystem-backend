"""家長端聯絡簿 (api/parent_portal/contact_book.py) 對應 Out schemas。

Phase 3.5 範圍（本檔）：
- GET    /parent/contact-book/today                 → ParentContactBookTodayOut
- GET    /parent/contact-book                       → ParentContactBookHistoryOut
- GET    /parent/contact-book/{id}                  → ParentContactBookDetailOut
- POST   /parent/contact-book/{id}/ack              → ParentContactBookAckOut
- POST   /parent/contact-book/{id}/reply            → ParentContactBookReplyOut
- DELETE /parent/contact-book/{id}/replies/{rid}    → DeleteResultOut (共用)

與教師端 schemas/portal_contact_book.py 命名區隔：
- 教師端 prefix: ContactBook
- 家長端 prefix: ParentContactBook
家長視角下 mood / teacher_note / 學童體溫等同屬 PII，但屬其法定代理人本人資料，
標 `# pii-allow:` 與 portal_contact_book.py / portal_students.py 慣例對齊。
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from schemas._base import IvyBaseModel
from schemas._common import DeleteResultOut as DeleteResultOut  # re-export 方便 router

# ──────────────────────────────────────────────────────────────────────
# Photo / Entry building blocks（多 endpoint 共用）
# ──────────────────────────────────────────────────────────────────────


class ParentContactBookPhotoOut(IvyBaseModel):
    """單張照片回傳（家長視角，只給 display / thumb url，不含 original_filename）。"""

    id: int
    display_url: Optional[str] = None
    thumb_url: Optional[str] = None


class ParentContactBookEntryOut(IvyBaseModel):
    """聯絡簿單筆 entry payload（_entry_to_dict 序列化結果，家長視角）。

    回傳於：
    - GET /today（內嵌於 ParentContactBookTodayOut.entry）
    - GET /（歷史清單內每筆）
    - GET /{id}（被 ParentContactBookDetailOut 繼承）
    """

    id: int
    student_id: int
    classroom_id: int
    log_date: Optional[str] = None
    mood: Optional[str] = None  # pii-allow: 學童情緒（家長必看）
    meal_lunch: Optional[int] = None
    meal_snack: Optional[int] = None
    nap_minutes: Optional[int] = None
    bowel: Optional[str] = None
    temperature_c: Optional[float] = None  # pii-allow: 學童體溫
    teacher_note: Optional[str] = None  # pii-allow: 教師備註（含學童觀察）
    learning_highlight: Optional[str] = None  # pii-allow: 學習亮點
    published_at: Optional[str] = None
    my_acknowledged_at: Optional[str] = None
    photos: list[ParentContactBookPhotoOut] = []


# ──────────────────────────────────────────────────────────────────────
# Reply building block（detail / reply mutation 共用）
# ──────────────────────────────────────────────────────────────────────


class ParentContactBookReplyOut(IvyBaseModel):
    """家長回覆單筆（_reply_to_dict 序列化結果）。

    回傳於：
    - POST /{id}/reply（單筆新增 / idempotent replay）
    - 被 ParentContactBookDetailOut.replies 內嵌
    """

    id: int
    entry_id: int
    guardian_user_id: int  # pii-allow: 家長自身 user_id（前端識別自己的回覆）
    body: str  # pii-allow: 家長回覆內容（自身資料）
    created_at: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
# GET /today → ParentContactBookTodayOut
# ──────────────────────────────────────────────────────────────────────


class ParentContactBookTodayOut(IvyBaseModel):
    """GET /parent/contact-book/today — 今日聯絡簿（沒有 / 未發布時 entry=null）。"""

    student_id: int
    log_date: str
    entry: Optional[ParentContactBookEntryOut] = None


# ──────────────────────────────────────────────────────────────────────
# GET / → ParentContactBookHistoryOut
# ──────────────────────────────────────────────────────────────────────


class ParentContactBookHistoryOut(IvyBaseModel):
    """GET /parent/contact-book — 歷史清單（僅已發布）。

    Router 回傳的 dict key 為 "from" / "to"（Python 保留字無法作欄位名），
    透過 Field(alias=...) + serialization_alias 對齊；IvyBaseModel 預設
    `populate_by_name=True` 允許 alias 與原名雙向 populate。
    """

    student_id: int
    from_date: str = Field(alias="from", serialization_alias="from")
    to_date: str = Field(alias="to", serialization_alias="to")
    entries: list[ParentContactBookEntryOut]


# ──────────────────────────────────────────────────────────────────────
# GET /{id} → ParentContactBookDetailOut
# ──────────────────────────────────────────────────────────────────────


class ParentContactBookDetailOut(ParentContactBookEntryOut):
    """GET /parent/contact-book/{id} — 單筆詳情（entry 欄位 + replies 列表）。"""

    replies: list[ParentContactBookReplyOut] = []


# ──────────────────────────────────────────────────────────────────────
# POST /{id}/ack → ParentContactBookAckOut
# ──────────────────────────────────────────────────────────────────────


class ParentContactBookAckOut(IvyBaseModel):
    """POST /parent/contact-book/{id}/ack — 已讀回傳（idempotent）。"""

    entry_id: int
    read_at: Optional[str] = None
    already_marked: bool
