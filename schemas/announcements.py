"""Announcements router (api/announcements.py) Out schemas — Phase 3.5。

涵蓋 6 個 grandfather endpoint：

- GET    /announcements                                → AnnouncementListOut
- POST   /announcements                                → MutationResultOut (re-use)
- PUT    /announcements/{id}                           → DeleteResultOut (message-only)
- DELETE /announcements/{id}                           → DeleteResultOut (re-use)
- GET    /announcements/{id}/parent-recipients         → AnnouncementParentRecipientsOut
- PUT    /announcements/{id}/parent-recipients         → AnnouncementParentRecipientsOut

家長端對象設定（parent-recipients）回傳 `{announcement_id, items, total}` envelope
shape，list/replace 共用同一個 Out。

PII 註解：
- ``read_preview`` / ``readers`` 內含員工姓名（read_employee_map 帶出來）— 員工
  端 ANNOUNCEMENTS_READ 必看，標 ``# pii-allow``。
- ``created_by_name`` 為公告作者姓名 — 同上 ``# pii-allow``。
- ``classroom_id`` / ``student_id`` / ``guardian_id`` 皆為 FK 引用（非個人 PII），
  標 ``# pii-allow: FK 引用 (非個人 PII)``，沿用 schemas/portal_students.py 慣例。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from schemas._base import IvyBaseModel


class AnnouncementReaderItemOut(IvyBaseModel):
    """單筆已讀者明細（list_announcements 內嵌）。"""

    employee_id: Optional[int] = None
    name: str  # pii-allow: 員工姓名（ANNOUNCEMENTS_READ 必看）
    read_at: Optional[str] = None


class AnnouncementItemOut(IvyBaseModel):
    """單筆公告（管理員列表用，含 readers/recipient 統計）。"""

    id: int
    title: str
    content: str
    priority: str
    is_pinned: bool
    created_by: Optional[int] = None
    created_by_name: str  # pii-allow: 公告作者姓名（ANNOUNCEMENTS_READ 必看）
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    publish_at: Optional[str] = None
    expires_at: Optional[str] = None
    status: Literal["scheduled", "active", "expired"]
    read_count: int
    read_preview: list[AnnouncementReaderItemOut]  # pii-allow: 已讀者姓名預覽（同上）
    has_more_readers: bool
    recipient_count: int


class AnnouncementListOut(IvyBaseModel):
    """GET /announcements 分頁回傳。"""

    total: int
    items: list[AnnouncementItemOut]


class AnnouncementParentRecipientItemOut(IvyBaseModel):
    """家長端對象單筆（_serialize_parent_recipient 對應）。"""

    id: int
    scope: str
    classroom_id: Optional[int] = None  # pii-allow: FK 引用 (非個人 PII)
    student_id: Optional[int] = None  # pii-allow: FK 引用 (非個人 PII)
    guardian_id: Optional[int] = None  # pii-allow: FK 引用 (非個人 PII)


class AnnouncementParentRecipientsOut(IvyBaseModel):
    """GET / PUT /announcements/{id}/parent-recipients 共用 envelope。"""

    announcement_id: int
    items: list[AnnouncementParentRecipientItemOut]
    total: int


class AnnouncementRecipientsOut(IvyBaseModel):
    """GET /announcements/{id}/recipients 回傳（lazy fetch admin edit dialog 用）。"""

    employee_ids: list[int]


class ReaderListItem(IvyBaseModel):
    """單筆已讀者明細（list_readers 端用，等同 AnnouncementReaderItemOut 但獨立暴露）。"""

    employee_id: int
    name: str  # pii-allow: 員工姓名（ANNOUNCEMENTS_READ 必看）
    read_at: Optional[str] = None


class AnnouncementReadersOut(IvyBaseModel):
    """GET /announcements/{id}/readers 分頁回傳。"""

    items: list[ReaderListItem]
    total: int
    page: int
    page_size: int


# Backward-compat re-export — POST /announcements 用 _common 的 {message, id}。
from schemas._common import (  # noqa: E402,F401
    DeleteResultOut as AnnouncementMessageOut,
)
from schemas._common import (
    MutationResultOut as AnnouncementCreateResultOut,
)  # noqa: E402,F401

# silence unused import warnings (re-export only)
_ = Any
