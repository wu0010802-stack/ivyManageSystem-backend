"""家長端訊息 (api/parent_portal/messages.py) Out schemas。

Phase 3 範圍：
- POST /threads/{id}/read → OkStatusOut (re-use)
- POST /messages/{id}/recall → MessageRecallOut
- GET /unread-count → UnreadCountOut (re-use)

Phase 3.5 範圍（本檔新增）：
- GET    /threads                                    → ParentPortalMessageThreadListOut
- GET    /threads/{id}                               → ParentPortalMessageThreadOut
- GET    /threads/{id}/messages                      → ParentPortalMessageListOut
- POST   /threads/{id}/messages                      → ParentPortalMessageReplyOut
- POST   /threads/{id}/messages/{mid}/attach         → ParentPortalMessageAttachmentOut

PII 標註：學童姓名 / 老師姓名 / 訊息 body / 訊息預覽
為家長端必看欄位，標 `pii-allow:` 與 Sentry denylist exempt 對齊
（同 portal_parent_messages.py 慣例，僅 parent/teacher 視角互換）。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel


class MessageRecallOut(IvyBaseModel):
    """POST /messages/{id}/recall — 撤回訊息回傳 {status, deleted_at}。"""

    status: str
    deleted_at: str


# ──────────────────────────────────────────────────────────────────────
# Building blocks（多 endpoint 共用）
# ──────────────────────────────────────────────────────────────────────


class ParentPortalMessageAttachmentOut(IvyBaseModel):
    """訊息附件單筆（_attachment_to_dict 序列化結果）。

    回傳於：
    - POST /threads/{id}/messages/{mid}/attach 的 response（單張）
    - 被 ParentPortalMessageOut.attachments 內嵌
    """

    id: int
    original_filename: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    url: Optional[str] = None
    thumb_url: Optional[str] = None
    display_url: Optional[str] = None


class ParentPortalMessageOut(IvyBaseModel):
    """單則訊息（_message_to_dict 序列化結果）。

    回傳於：
    - ParentPortalMessageListOut.items 元素
    - ParentPortalMessageReplyOut 透過繼承共用欄位
    """

    id: int
    thread_id: int
    sender_user_id: int
    sender_role: str
    body: Optional[str] = None  # pii-allow: 訊息內容（家長端必看；deleted 時為 None）
    deleted: bool
    source: str
    client_request_id: Optional[str] = None
    attachments: list[ParentPortalMessageAttachmentOut] = []
    created_at: Optional[str] = None


class ParentPortalMessageThreadOut(IvyBaseModel):
    """單筆 thread summary（_thread_summary 序列化結果）。

    回傳於：
    - GET /threads/{id}（單筆）
    - ParentPortalMessageThreadListOut.items 元素
    """

    id: int
    student_id: int
    student_name: Optional[str] = None  # pii-allow: 學童姓名（家長端必看）
    teacher_user_id: int
    teacher_name: Optional[str] = None  # pii-allow: 老師姓名（家長端必看）
    last_message_at: Optional[str] = None
    last_message_preview: Optional[str] = None  # pii-allow: 訊息預覽（家長端必看）
    unread_count: int


# ──────────────────────────────────────────────────────────────────────
# GET /threads → ParentPortalMessageThreadListOut
# ──────────────────────────────────────────────────────────────────────


class ParentPortalMessageThreadListOut(IvyBaseModel):
    """GET /messages/threads — cursor pagination thread 列表。"""

    items: list[ParentPortalMessageThreadOut]
    next_cursor: Optional[int] = None


# ──────────────────────────────────────────────────────────────────────
# GET /threads/{id}/messages → ParentPortalMessageListOut
# ──────────────────────────────────────────────────────────────────────


class ParentPortalMessageListOut(IvyBaseModel):
    """GET /messages/threads/{id}/messages — cursor pagination 訊息列表（DESC by id）。"""

    items: list[ParentPortalMessageOut]
    next_cursor: Optional[int] = None


# ──────────────────────────────────────────────────────────────────────
# POST /threads/{id}/messages → ParentPortalMessageReplyOut
# ──────────────────────────────────────────────────────────────────────


class ParentPortalMessageReplyOut(ParentPortalMessageOut):
    """POST /messages/threads/{id}/messages — 家長回覆。

    Flat shape：繼承 ParentPortalMessageOut 所有欄位 + idempotent_replay。
    對應 router 端 `{**_message_to_dict(...), "idempotent_replay": replayed}`。
    """

    idempotent_replay: bool
