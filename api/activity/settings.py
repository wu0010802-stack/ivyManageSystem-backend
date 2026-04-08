"""
api/activity/settings.py — 報名時間設定 + class-options + changes（4 個端點）
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import get_session, ActivityRegistrationSettings, RegistrationChange, Classroom
from utils.auth import require_permission
from utils.permissions import Permission

from ._shared import RegistrationTimeSettings

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/settings/registration-time")
async def get_registration_time(
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得報名開放設定（管理後台用，需認證）"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            return {"is_open": False, "open_at": None, "close_at": None}
        return {
            "is_open": settings.is_open,
            "open_at": settings.open_at,
            "close_at": settings.close_at,
        }
    finally:
        session.close()


@router.post("/settings/registration-time")
async def update_registration_time(
    body: RegistrationTimeSettings,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """更新報名開放設定"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            settings = ActivityRegistrationSettings()
            session.add(settings)

        settings.is_open = body.is_open
        settings.open_at = body.open_at
        settings.close_at = body.close_at
        session.commit()
        return {"message": "報名時間設定已更新"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/changes")
async def get_changes(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得修改紀錄列表"""
    session = get_session()
    try:
        q = session.query(RegistrationChange)
        total = q.count()
        rows = (
            q.order_by(RegistrationChange.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
        items = [
            {
                "id": r.id,
                "registration_id": r.registration_id,
                "student_name": r.student_name,
                "change_type": r.change_type,
                "description": r.description,
                "changed_by": r.changed_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return {"items": items, "total": total}
    finally:
        session.close()


@router.get("/class-options")
async def get_class_options(
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """從 Classroom 表動態取得班級名稱選項"""
    session = get_session()
    try:
        classrooms = (
            session.query(Classroom)
            .filter(Classroom.is_active.is_(True))
            .order_by(Classroom.id)
            .all()
        )
        return {"options": [c.name for c in classrooms]}
    finally:
        session.close()
