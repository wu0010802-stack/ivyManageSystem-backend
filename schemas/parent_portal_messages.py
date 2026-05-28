"""家長端訊息 (api/parent_portal/messages.py) Out schemas。

Phase 3 範圍（本檔）：
- POST /threads/{id}/read → OkStatusOut (re-use)
- POST /messages/{id}/recall → MessageRecallOut
- GET /unread-count → UnreadCountOut (re-use)

Out of scope (Phase 3.5)：
- GET /threads + /threads/{id}/messages (list + cursor pagination)
- POST /threads/{id}/messages + /attach (含 photo upload 複雜 shape)
- GET /threads/{id} 詳情
"""

from __future__ import annotations

from schemas._base import IvyBaseModel


class MessageRecallOut(IvyBaseModel):
    """POST /messages/{id}/recall — 撤回訊息回傳 {status, deleted_at}。"""

    status: str
    deleted_at: str
