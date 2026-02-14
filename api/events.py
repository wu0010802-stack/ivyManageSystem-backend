"""
School Events (Calendar) router - CRUD for school calendar events
"""

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from models.database import get_session, SchoolEvent
from utils.auth import get_current_user, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["events"])

EVENT_TYPE_LABELS = {
    "meeting": "會議",
    "activity": "活動",
    "holiday": "假日",
    "general": "一般",
}


# ============ Pydantic Models ============

class EventCreate(BaseModel):
    title: str
    description: Optional[str] = None
    event_date: date
    end_date: Optional[date] = None
    event_type: str = "general"
    is_all_day: bool = True
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None


class EventUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    event_date: Optional[date] = None
    end_date: Optional[date] = None
    event_type: Optional[str] = None
    is_all_day: Optional[bool] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: Optional[str] = None


# ============ Endpoints ============

def _event_to_dict(ev: SchoolEvent) -> dict:
    return {
        "id": ev.id,
        "title": ev.title,
        "description": ev.description,
        "event_date": ev.event_date.isoformat(),
        "end_date": ev.end_date.isoformat() if ev.end_date else None,
        "event_type": ev.event_type,
        "event_type_label": EVENT_TYPE_LABELS.get(ev.event_type, ev.event_type),
        "is_all_day": ev.is_all_day,
        "start_time": ev.start_time,
        "end_time": ev.end_time,
        "location": ev.location,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
        "updated_at": ev.updated_at.isoformat() if ev.updated_at else None,
    }


@router.get("/events")
def get_events(
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    event_type: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """取得行事曆事件列表"""
    session = get_session()
    try:
        q = session.query(SchoolEvent).filter(SchoolEvent.is_active == True)

        if year:
            start = date(year, month or 1, 1)
            if month:
                import calendar as cal_module
                _, last_day = cal_module.monthrange(year, month)
                end = date(year, month, last_day)
            else:
                end = date(year, 12, 31)
            # Include events that overlap with the range
            q = q.filter(
                SchoolEvent.event_date <= end,
                (SchoolEvent.end_date >= start) | (SchoolEvent.end_date.is_(None) & (SchoolEvent.event_date >= start)),
            )

        if event_type:
            q = q.filter(SchoolEvent.event_type == event_type)

        events = q.order_by(SchoolEvent.event_date).all()
        return [_event_to_dict(ev) for ev in events]
    finally:
        session.close()


@router.get("/events/{event_id}")
def get_event(event_id: int, current_user: dict = Depends(get_current_user)):
    """取得單一事件"""
    session = get_session()
    try:
        ev = session.query(SchoolEvent).filter(
            SchoolEvent.id == event_id,
            SchoolEvent.is_active == True,
        ).first()
        if not ev:
            raise HTTPException(status_code=404, detail="找不到該事件")
        return _event_to_dict(ev)
    finally:
        session.close()


@router.post("/events", status_code=201)
def create_event(data: EventCreate, current_user: dict = Depends(require_admin)):
    """新增行事曆事件"""
    session = get_session()
    try:
        if data.event_type not in EVENT_TYPE_LABELS:
            raise HTTPException(status_code=400, detail=f"無效的事件類型: {data.event_type}")
        if data.end_date and data.end_date < data.event_date:
            raise HTTPException(status_code=400, detail="結束日期不可早於開始日期")

        ev = SchoolEvent(
            title=data.title,
            description=data.description,
            event_date=data.event_date,
            end_date=data.end_date,
            event_type=data.event_type,
            is_all_day=data.is_all_day,
            start_time=data.start_time,
            end_time=data.end_time,
            location=data.location,
        )
        session.add(ev)
        session.commit()
        return {"message": "事件已建立", "id": ev.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/events/{event_id}")
def update_event(event_id: int, data: EventUpdate, current_user: dict = Depends(require_admin)):
    """更新行事曆事件"""
    session = get_session()
    try:
        ev = session.query(SchoolEvent).filter(
            SchoolEvent.id == event_id,
            SchoolEvent.is_active == True,
        ).first()
        if not ev:
            raise HTTPException(status_code=404, detail="找不到該事件")

        update_data = data.dict(exclude_unset=True)
        if "event_type" in update_data and update_data["event_type"] not in EVENT_TYPE_LABELS:
            raise HTTPException(status_code=400, detail=f"無效的事件類型: {update_data['event_type']}")

        for key, value in update_data.items():
            setattr(ev, key, value)

        # Validate end_date
        if ev.end_date and ev.end_date < ev.event_date:
            raise HTTPException(status_code=400, detail="結束日期不可早於開始日期")

        session.commit()
        return {"message": "事件已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/events/{event_id}")
def delete_event(event_id: int, current_user: dict = Depends(require_admin)):
    """刪除行事曆事件（軟刪除）"""
    session = get_session()
    try:
        ev = session.query(SchoolEvent).filter(
            SchoolEvent.id == event_id,
            SchoolEvent.is_active == True,
        ).first()
        if not ev:
            raise HTTPException(status_code=404, detail="找不到該事件")
        ev.is_active = False
        session.commit()
        return {"message": "事件已刪除"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
