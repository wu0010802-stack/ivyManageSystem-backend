"""api/parent_portal/parent_downloads.py — 家長端附件下載

staff 端走 /api/uploads/portfolio/{key}（utils PORTFOLIO_READ 權限），家長角色
mask=0 無此 bit；故另開家長下載路徑：/api/parent/uploads/portfolio/{key}
走 require_parent_role + IDOR（owner 反查 student → _assert_student_owned）。

支援的 owner_type：medication_order、event_acknowledgment、message（Phase 3）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response

from models.database import (
    Attachment,
    EventAcknowledgment,
    ParentMessage,
    ParentMessageThread,
    StudentMedicationOrder,
    get_session,
)
from models.portfolio import (
    ATTACHMENT_OWNER_EVENT_ACK,
    ATTACHMENT_OWNER_MEDICATION_ORDER,
    ATTACHMENT_OWNER_MESSAGE,
)
from utils.auth import require_parent_role
from utils.portfolio_storage import get_portfolio_storage

from ._shared import _assert_student_owned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/uploads", tags=["parent-uploads"])


def _resolve_student_id_for_parent(session, owner_type: str, owner_id: int) -> int:
    """反查 attachment owner → student_id，給 IDOR 用。

    Phase 2 支援 medication_order / event_acknowledgment；
    Phase 3 message 待新表完成後補。
    """
    if owner_type == ATTACHMENT_OWNER_MEDICATION_ORDER:
        o = (
            session.query(StudentMedicationOrder)
            .filter(StudentMedicationOrder.id == owner_id)
            .first()
        )
        if not o:
            raise HTTPException(status_code=404, detail="對應的用藥單不存在")
        return o.student_id

    if owner_type == ATTACHMENT_OWNER_EVENT_ACK:
        ack = (
            session.query(EventAcknowledgment)
            .filter(EventAcknowledgment.id == owner_id)
            .first()
        )
        if not ack:
            raise HTTPException(status_code=404, detail="對應的簽收紀錄不存在")
        return ack.student_id

    if owner_type == ATTACHMENT_OWNER_MESSAGE:
        msg = session.query(ParentMessage).filter(ParentMessage.id == owner_id).first()
        if not msg:
            raise HTTPException(status_code=404, detail="對應的訊息不存在")
        thread = (
            session.query(ParentMessageThread)
            .filter(ParentMessageThread.id == msg.thread_id)
            .first()
        )
        if not thread:
            raise HTTPException(status_code=404, detail="訊息對應的 thread 不存在")
        return thread.student_id

    raise HTTPException(status_code=400, detail=f"不支援的 owner_type：{owner_type}")


@router.get("/portfolio/{key:path}")
def download_parent_portfolio(
    key: str,
    current_user: dict = Depends(require_parent_role()),
) -> Response:
    """家長下載自己孩子相關的附件。

    流程：
    1. 依 key 反查 attachments（同時匹配 storage_key / display_key / thumb_key）
    2. 檢查軟刪除
    3. 反查 owner 的 student_id → _assert_student_owned IDOR 守衛
    4. 回傳檔案
    """
    user_id = current_user["user_id"]
    session = get_session()
    try:
        att = (
            session.query(Attachment)
            .filter(
                (Attachment.storage_key == key)
                | (Attachment.display_key == key)
                | (Attachment.thumb_key == key)
            )
            .first()
        )
        if not att:
            raise HTTPException(status_code=404, detail="檔案不存在")
        if att.deleted_at:
            raise HTTPException(status_code=410, detail="檔案已刪除")

        student_id = _resolve_student_id_for_parent(
            session, att.owner_type, att.owner_id
        )
        _assert_student_owned(session, user_id, student_id)

        storage = get_portfolio_storage()
        path = storage.absolute_path(key)
        if not path.exists():
            logger.error("家長下載：實體檔案遺失 key=%s attachment_id=%d", key, att.id)
            raise HTTPException(status_code=404, detail="檔案實體遺失")

        # storage_key 用原始 mime；display/thumb 為 JPG
        mime = att.mime_type if key == att.storage_key else "image/jpeg"
        return FileResponse(
            path=str(path),
            media_type=mime,
            filename=att.original_filename if key == att.storage_key else None,
        )
    finally:
        session.close()
