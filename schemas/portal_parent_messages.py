"""教師端家園溝通 (api/portal/parent_messages.py) 對應 Out schemas。

Phase 3.5 範圍（本檔）：
- GET    /portal/parent-messages/threads                          → PortalParentMessageThreadListOut
- GET    /portal/parent-messages/threads/{id}                     → PortalParentMessageThreadOut
- GET    /portal/parent-messages/threads/{id}/messages            → PortalParentMessageListOut
- POST   /portal/parent-messages/threads                          → PortalParentMessageCreateThreadOut
- POST   /portal/parent-messages/threads/{id}/messages            → PortalParentMessageReplyOut
- POST   /portal/parent-messages/threads/{id}/messages/{mid}/attach → PortalParentMessageAttachmentOut

已在他處（不重複）：
- POST /threads/{id}/read     → schemas._common.OkStatusOut
- POST /messages/{id}/recall  → schemas.parent_portal_messages.MessageRecallOut
- GET  /unread-count          → schemas._common.UnreadCountOut

PII 標註：學童姓名 / 家長姓名（User.username） / 訊息 body / 訊息預覽
為教師端必看欄位，標 `pii-allow:` 與 Sentry denylist exempt 對齊
（同 portal_contact_book.py 慣例）。
"""

from __future__ import annotations

from typing import Optional

from schemas._base import IvyBaseModel

# ──────────────────────────────────────────────────────────────────────
# Building blocks（多 endpoint 共用）
# ──────────────────────────────────────────────────────────────────────


class PortalParentMessageAttachmentOut(IvyBaseModel):
    """訊息附件單筆（_attachment_to_dict 序列化結果）。

    回傳於：
    - POST /threads/{id}/messages/{mid}/attach 的 response（單張）
    - 被 PortalParentMessageOut.attachments 內嵌
    """

    id: int
    original_filename: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    url: Optional[str] = None
    thumb_url: Optional[str] = None
    display_url: Optional[str] = None


class PortalParentMessageOut(IvyBaseModel):
    """單則訊息（_message_to_dict 序列化結果）。

    回傳於：
    - PortalParentMessageListOut.items 元素
    - PortalParentMessageCreateThreadOut.message
    - PortalParentMessageReplyOut 透過繼承共用欄位
    """

    id: int
    thread_id: int
    sender_user_id: int
    sender_role: str
    body: Optional[str] = None  # pii-allow: 訊息內容（教師端必看；deleted 時為 None）
    deleted: bool
    source: str
    client_request_id: Optional[str] = None
    attachments: list[PortalParentMessageAttachmentOut] = []
    created_at: Optional[str] = None


class PortalParentMessageThreadOut(IvyBaseModel):
    """單筆 thread summary（_thread_summary 序列化結果）。

    回傳於：
    - GET /threads/{id}（單筆）
    - PortalParentMessageThreadListOut.items 元素
    - PortalParentMessageCreateThreadOut.thread
    """

    id: int
    student_id: int
    student_name: Optional[str] = None  # pii-allow: 學童姓名（教師端必看）
    parent_user_id: int
    parent_name: Optional[str] = None  # pii-allow: 家長姓名（User.username）
    last_message_at: Optional[str] = None
    last_message_preview: Optional[str] = None  # pii-allow: 訊息預覽（教師端必看）
    unread_count: int


# ──────────────────────────────────────────────────────────────────────
# GET /threads → PortalParentMessageThreadListOut
# ──────────────────────────────────────────────────────────────────────


class PortalParentMessageThreadListOut(IvyBaseModel):
    """GET /portal/parent-messages/threads — cursor pagination thread 列表。"""

    items: list[PortalParentMessageThreadOut]
    next_cursor: Optional[int] = None


# ──────────────────────────────────────────────────────────────────────
# GET /threads/{id}/messages → PortalParentMessageListOut
# ──────────────────────────────────────────────────────────────────────


class PortalParentMessageListOut(IvyBaseModel):
    """GET /portal/parent-messages/threads/{id}/messages — cursor pagination
    訊息列表（DESC by id）。"""

    items: list[PortalParentMessageOut]
    next_cursor: Optional[int] = None


# ──────────────────────────────────────────────────────────────────────
# POST /threads → PortalParentMessageCreateThreadOut
# ──────────────────────────────────────────────────────────────────────


class PortalParentMessageCreateThreadOut(IvyBaseModel):
    """POST /portal/parent-messages/threads — 教師發起新 thread + 第一則訊息。

    嵌套 shape：thread summary + 第一則 message + idempotent_replay。
    """

    thread: PortalParentMessageThreadOut
    message: PortalParentMessageOut
    idempotent_replay: bool


# ──────────────────────────────────────────────────────────────────────
# POST /threads/{id}/messages → PortalParentMessageReplyOut
# ──────────────────────────────────────────────────────────────────────


class PortalParentMessageReplyOut(PortalParentMessageOut):
    """POST /portal/parent-messages/threads/{id}/messages — 教師回覆。

    Flat shape：繼承 PortalParentMessageOut 所有欄位 + idempotent_replay。
    對應 router 端 `{**_message_to_dict(...), "idempotent_replay": replayed}`。
    """

    idempotent_replay: bool
