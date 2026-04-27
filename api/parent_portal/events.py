"""api/parent_portal/events.py — 家長端學校行事曆事件 + 簽閱。

- GET /api/parent/events：家長可見事件清單（過去 30 天 + 未來 180 天），
  附加 `requires_acknowledgment` 與「家長對哪幾個小孩已簽 / 未簽」資訊
- POST /api/parent/events/{event_id}/ack：簽閱（指定哪一個小孩簽）
"""

from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from models.database import EventAcknowledgment, SchoolEvent, get_session
from utils.auth import require_parent_role

from ._shared import _assert_student_owned, _get_parent_student_ids

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
            "acknowledged_at": ack.acknowledged_at.isoformat(),
        }
    finally:
        session.close()
