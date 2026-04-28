"""api/parent_portal/events.py — 家長端學校行事曆事件 + 簽閱。

- GET /api/parent/events：家長可見事件清單（過去 30 天 + 未來 180 天），
  附加 `requires_acknowledgment` 與「家長對哪幾個小孩已簽 / 未簽」資訊
- POST /api/parent/events/{event_id}/ack：簽閱（指定哪一個小孩簽）
- POST /api/parent/events/{event_id}/ack/signature：上傳手寫簽名 PNG
  （兩段式：先 ack 取得 ack_id，再上傳簽名圖；簽名圖可重新覆蓋）
"""

import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field

from models.database import (
    Attachment,
    EventAcknowledgment,
    SchoolEvent,
    get_session,
)
from models.portfolio import ATTACHMENT_OWNER_EVENT_ACK
from utils.auth import require_parent_role
from utils.file_upload import validate_file_signature

from ._shared import _assert_student_owned, _get_parent_student_ids

logger = logging.getLogger(__name__)

# 簽名圖大小硬上限：200KB（canvas toBlob 通常 5-30KB；防止濫用）
_SIGNATURE_MAX_BYTES = 200 * 1024
_SIGNATURE_ALLOWED_EXT = {".png"}

router = APIRouter(prefix="/events", tags=["parent-events"])

_PAST_DAYS = 30
_FUTURE_DAYS = 180


class AckRequest(BaseModel):
    student_id: int = Field(..., gt=0)
    signature_name: Optional[str] = Field(None, max_length=50)


def count_pending_acks_for_user(session, user_id: int, student_ids: list[int]) -> int:
    """家長尚未簽閱的 (event, student) 配對數。

    用於 home/summary：例如有 2 個學生 × 1 個未簽 event = 2。
    時間窗 = 過去 30 天 + 未來 180 天，與 list_events 相同。
    """
    if not student_ids:
        return 0
    today = date.today()
    df = today - timedelta(days=_PAST_DAYS)
    dt = today + timedelta(days=_FUTURE_DAYS)
    events = (
        session.query(SchoolEvent.id)
        .filter(
            SchoolEvent.is_active == True,  # noqa: E712
            SchoolEvent.requires_acknowledgment == True,  # noqa: E712
            SchoolEvent.event_date >= df,
            SchoolEvent.event_date <= dt,
        )
        .all()
    )
    event_ids = [e[0] for e in events]
    if not event_ids:
        return 0
    acked_pairs = set()
    rows = (
        session.query(EventAcknowledgment.event_id, EventAcknowledgment.student_id)
        .filter(
            EventAcknowledgment.user_id == user_id,
            EventAcknowledgment.event_id.in_(event_ids),
            EventAcknowledgment.student_id.in_(student_ids),
        )
        .all()
    )
    for ev_id, st_id in rows:
        acked_pairs.add((ev_id, st_id))
    pending = 0
    for ev_id in event_ids:
        for st_id in student_ids:
            if (ev_id, st_id) not in acked_pairs:
                pending += 1
    return pending


@router.get("")
def list_events(current_user: dict = Depends(require_parent_role())):
    user_id = current_user["user_id"]
    today = date.today()
    df = today - timedelta(days=_PAST_DAYS)
    dt = today + timedelta(days=_FUTURE_DAYS)
    session = get_session()
    try:
        _, student_ids = _get_parent_student_ids(session, user_id)

        events = (
            session.query(SchoolEvent)
            .filter(
                SchoolEvent.is_active == True,
                SchoolEvent.event_date >= df,
                SchoolEvent.event_date <= dt,
            )
            .order_by(SchoolEvent.event_date.asc())
            .all()
        )
        event_ids = [e.id for e in events]

        ack_map: dict[int, set[int]] = {}
        if event_ids and student_ids:
            ack_rows = (
                session.query(
                    EventAcknowledgment.event_id, EventAcknowledgment.student_id
                )
                .filter(
                    EventAcknowledgment.user_id == user_id,
                    EventAcknowledgment.event_id.in_(event_ids),
                    EventAcknowledgment.student_id.in_(student_ids),
                )
                .all()
            )
            for ev_id, st_id in ack_rows:
                ack_map.setdefault(ev_id, set()).add(st_id)

        items = []
        for e in events:
            acked_for = sorted(ack_map.get(e.id, set()))
            need_ack_for = (
                sorted(set(student_ids) - set(acked_for))
                if e.requires_acknowledgment
                else []
            )
            items.append(
                {
                    "id": e.id,
                    "title": e.title,
                    "description": e.description,
                    "event_date": e.event_date.isoformat() if e.event_date else None,
                    "end_date": e.end_date.isoformat() if e.end_date else None,
                    "event_type": e.event_type,
                    "is_all_day": bool(e.is_all_day),
                    "start_time": e.start_time,
                    "end_time": e.end_time,
                    "location": e.location,
                    "requires_acknowledgment": bool(e.requires_acknowledgment),
                    "ack_deadline": (
                        e.ack_deadline.isoformat() if e.ack_deadline else None
                    ),
                    "acked_student_ids": acked_for,
                    "need_ack_student_ids": need_ack_for,
                }
            )
        return {"items": items, "total": len(items)}
    finally:
        session.close()


@router.post("/{event_id}/ack", status_code=200)
def acknowledge_event(
    event_id: int,
    payload: AckRequest,
    current_user: dict = Depends(require_parent_role()),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        _assert_student_owned(session, user_id, payload.student_id)

        event = (
            session.query(SchoolEvent)
            .filter(SchoolEvent.id == event_id, SchoolEvent.is_active == True)
            .first()
        )
        if event is None:
            raise HTTPException(status_code=404, detail="找不到事件")
        if not event.requires_acknowledgment:
            raise HTTPException(status_code=400, detail="此事件未要求簽閱")

        existing = (
            session.query(EventAcknowledgment)
            .filter(
                EventAcknowledgment.event_id == event_id,
                EventAcknowledgment.user_id == user_id,
                EventAcknowledgment.student_id == payload.student_id,
            )
            .first()
        )
        if existing is not None:
            return {
                "status": "ok",
                "already_acknowledged": True,
                "acknowledged_at": existing.acknowledged_at.isoformat(),
            }

        ack = EventAcknowledgment(
            event_id=event_id,
            user_id=user_id,
            student_id=payload.student_id,
            acknowledged_at=datetime.now(),
            signature_name=(payload.signature_name or "").strip() or None,
        )
        session.add(ack)
        session.commit()
        return {
            "status": "ok",
            "already_acknowledged": False,
            "ack_id": ack.id,
            "acknowledged_at": ack.acknowledged_at.isoformat(),
        }
    finally:
        session.close()


@router.post("/{event_id}/ack/signature", status_code=201)
async def upload_ack_signature(
    event_id: int,
    request: Request,
    student_id: int = Query(..., gt=0),
    file: UploadFile = File(...),
    current_user: dict = Depends(require_parent_role()),
):
    """上傳已建立的簽收紀錄之手寫簽名圖（PNG）。

    重複上傳會將先前的 attachment 軟刪除並換成新檔（家長簽錯可重簽）。
    """
    user_id = current_user["user_id"]

    filename = file.filename or "signature.png"
    ext = os.path.splitext(filename)[1].lower() or ".png"
    if ext not in _SIGNATURE_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="簽名檔僅接受 PNG")

    # 不沿用 read_upload_with_size_check 預設 10MB 上限：簽名圖過大代表非預期使用
    content = await file.read()
    if len(content) > _SIGNATURE_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"簽名圖過大（{len(content)} bytes，上限 {_SIGNATURE_MAX_BYTES}）",
        )
    validate_file_signature(content, ext)

    from utils.portfolio_storage import get_portfolio_storage

    session = get_session()
    try:
        _assert_student_owned(session, user_id, student_id)
        ack = (
            session.query(EventAcknowledgment)
            .filter(
                EventAcknowledgment.event_id == event_id,
                EventAcknowledgment.user_id == user_id,
                EventAcknowledgment.student_id == student_id,
            )
            .first()
        )
        if ack is None:
            raise HTTPException(
                status_code=404,
                detail="尚未簽收此事件，請先 POST /ack 後再上傳簽名",
            )

        # 若已有舊簽名，軟刪除以保留歷史
        if ack.signature_attachment_id:
            old = (
                session.query(Attachment)
                .filter(Attachment.id == ack.signature_attachment_id)
                .first()
            )
            if old and not old.deleted_at:
                old.deleted_at = datetime.now()

        storage = get_portfolio_storage()
        stored = storage.put_attachment(content, ext)
        att = Attachment(
            owner_type=ATTACHMENT_OWNER_EVENT_ACK,
            owner_id=ack.id,
            storage_key=stored.storage_key,
            display_key=stored.display_key,
            thumb_key=stored.thumb_key,
            original_filename=filename,
            mime_type=stored.mime_type,
            size_bytes=len(content),
            uploaded_by=user_id,
        )
        session.add(att)
        session.flush()
        session.refresh(att)
        ack.signature_attachment_id = att.id
        session.commit()

        request.state.audit_entity_id = str(ack.id)
        request.state.audit_summary = (
            f"家長上傳事件簽名：event_id={event_id} student_id={student_id} "
            f"ack_id={ack.id} attachment_id={att.id} size={len(content)}B"
        )
        logger.info(
            "家長上傳簽名：event_id=%d student_id=%d ack_id=%d att_id=%d size=%d",
            event_id,
            student_id,
            ack.id,
            att.id,
            len(content),
        )
        return {
            "ack_id": ack.id,
            "signature_attachment_id": att.id,
            "url": f"/api/parent/uploads/portfolio/{att.storage_key}",
            "thumb_url": (
                f"/api/parent/uploads/portfolio/{att.thumb_key}"
                if att.thumb_key
                else None
            ),
        }
    finally:
        session.close()
