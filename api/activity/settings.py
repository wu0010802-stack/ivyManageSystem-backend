"""
api/activity/settings.py — 報名時間設定 + class-options + changes + 海報上傳
"""

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from models.database import (
    get_session,
    ActivityRegistrationSettings,
    RegistrationChange,
    Classroom,
)
from utils.auth import require_staff_permission
from utils.file_upload import read_upload_with_size_check, validate_file_signature
from utils.permissions import Permission
from utils.storage import get_storage_path

from ._shared import RegistrationTimeSettings

_POSTER_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_POSTER_MODULE = "activity_posters"


def _poster_dir() -> Path:
    return get_storage_path(_POSTER_MODULE)


logger = logging.getLogger(__name__)
router = APIRouter()


_DISPLAY_FIELDS = (
    "page_title",
    "term_label",
    "event_date_label",
    "target_audience",
    "form_card_title",
    "poster_url",
)


def _serialize_settings(settings: ActivityRegistrationSettings | None) -> dict:
    """統一序列化（含時間與前台顯示欄位），未設定時回傳 None。"""
    if not settings:
        return {
            "is_open": False,
            "open_at": None,
            "close_at": None,
            **{k: None for k in _DISPLAY_FIELDS},
        }
    return {
        "is_open": settings.is_open,
        "open_at": settings.open_at,
        "close_at": settings.close_at,
        **{k: getattr(settings, k, None) for k in _DISPLAY_FIELDS},
    }


@router.get("/settings/registration-time")
async def get_registration_time(
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得報名開放設定與前台顯示設定（管理後台用）"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        return _serialize_settings(settings)
    finally:
        session.close()


@router.post("/settings/registration-time")
async def update_registration_time(
    body: RegistrationTimeSettings,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """更新報名開放設定與前台顯示設定"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            settings = ActivityRegistrationSettings()
            session.add(settings)

        settings.is_open = body.is_open
        settings.open_at = body.open_at
        settings.close_at = body.close_at
        for field in _DISPLAY_FIELDS:
            value = getattr(body, field, None)
            if isinstance(value, str):
                value = value.strip() or None
            setattr(settings, field, value)
        session.commit()
        return {"message": "報名時間設定已更新"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.post("/settings/poster")
async def upload_activity_poster(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """上傳活動海報圖，寫入 data/activity_posters 並更新 settings.poster_url。"""
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in _POSTER_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的檔案格式，允許：{'、'.join(sorted(_POSTER_ALLOWED_EXT))}",
        )

    content = await read_upload_with_size_check(file)
    # webp 無 magic bytes 條目，validate_file_signature 會略過；其餘會驗證
    validate_file_signature(content, ext)

    poster_dir = _poster_dir()
    stored_name = f"{uuid.uuid4().hex}{ext}"
    file_path = poster_dir / stored_name
    file_path.write_bytes(content)

    poster_url = f"/api/activity/public/poster/{stored_name}"

    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            settings = ActivityRegistrationSettings()
            session.add(settings)
        # 刪掉前一張避免 data 目錄無限長大
        old = settings.poster_url
        if old and old.startswith("/api/activity/public/poster/"):
            old_name = old.rsplit("/", 1)[-1]
            # 只允許刪 hex + 已知副檔名，防穿越
            if Path(old_name).suffix.lower() in _POSTER_ALLOWED_EXT:
                old_path = poster_dir / old_name
                if old_path.is_file():
                    try:
                        old_path.unlink()
                    except OSError as e:
                        logger.warning("刪除舊海報失敗：%s", e)
        settings.poster_url = poster_url
        session.commit()
        logger.info("活動海報已更新：%s", stored_name)
        return {"message": "海報已更新", "poster_url": poster_url}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/changes")
async def get_changes(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
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
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
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
