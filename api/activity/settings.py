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
from utils.errors import raise_safe_500
from utils.file_upload import read_upload_with_size_check
from utils.permissions import Permission

from ._shared import (
    RegistrationTimeSettings,
    desensitize_change_operator,
    has_payment_approve,
)
from schemas._common import DeleteResultOut
from schemas.activity_admin import (
    ActivityClassOptionsOut,
    ActivityPosterUploadResultOut,
    ActivityRegistrationChangeListOut,
    ActivityRegistrationTimeOut,
)

_POSTER_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_POSTER_MODULE = "activity_posters"


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


@router.get("/settings/registration-time", response_model=ActivityRegistrationTimeOut)
def get_registration_time(
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得報名開放設定與前台顯示設定（管理後台用）"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        return _serialize_settings(settings)
    finally:
        session.close()


@router.post("/settings/registration-time", response_model=DeleteResultOut)
def update_registration_time(
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
        raise_safe_500(e)
    finally:
        session.close()


@router.post("/settings/poster", response_model=ActivityPosterUploadResultOut)
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

    # Finding 檔Low-1：帶 extension=ext 才會走 validate + image EXIF strip。
    # webp 無 magic bytes 條目，validate 會略過，唯一防線是 strip 的 PIL 重解碼；
    # 不帶 extension 則兩者皆跳過 → 假 webp（HTML）原樣落盤再由公開端點回給訪客。
    content = await read_upload_with_size_check(file, extension=ext)

    from utils.storage import get_backend

    backend = get_backend()

    stored_name = f"{uuid.uuid4().hex}{ext}"
    content_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")
    backend.save(_POSTER_MODULE, stored_name, content, content_type)

    # 公開 URL：local 模式回 /api/activity/public/poster/<file>（不變）
    #          supabase 模式回 https://<project>.supabase.co/.../activity-posters/<file>
    poster_url = backend.public_url(_POSTER_MODULE, stored_name)

    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            settings = ActivityRegistrationSettings()
            session.add(settings)
        old = settings.poster_url
        settings.poster_url = poster_url
        session.commit()
        logger.info("活動海報已更新：%s", stored_name)
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()

    # Finding 7（2026-06-22）：舊檔的刪除須在 DB commit 成功「之後」。
    # 原本順序是「刪舊檔 → commit」，commit 失敗時 DB rollback 回舊 poster_url，
    # 但舊檔已被刪 → 現有海報永久失效、新檔成孤兒。改成 commit 成功才刪舊檔：
    # commit 失敗時舊檔完好（上面已 raise 不會走到這），新檔變孤兒（較輕代價）。
    if old and old != poster_url:
        # 從舊 URL 反推檔名（兩種來源：/api/activity/public/poster/<file> 或 https://.../<file>）
        old_name = old.rsplit("/", 1)[-1].split("?", 1)[0]
        # 只允許刪 hex + 已知副檔名，防穿越
        if Path(old_name).suffix.lower() in _POSTER_ALLOWED_EXT and len(old_name) < 80:
            try:
                backend.delete(_POSTER_MODULE, old_name)
            except Exception as e:
                logger.warning("刪除舊海報失敗：%s", e)

    return {"message": "海報已更新", "poster_url": poster_url}


@router.get("/changes", response_model=ActivityRegistrationChangeListOut)
def get_changes(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得修改紀錄列表"""
    session = get_session()
    try:
        # P1（2026-06-23 code review）：金流類 change 的 changed_by=經手人，須對非簽核者
        # 遮罩，與繳費明細 / POS 收據同口徑；否則低權限員工可從修改紀錄繞過列表遮罩。
        viewer_has_approve = has_payment_approve(current_user)
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
                "changed_by": desensitize_change_operator(
                    r.change_type, r.changed_by, viewer_has_approve
                ),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return {"items": items, "total": total}
    finally:
        session.close()


@router.get("/class-options", response_model=ActivityClassOptionsOut)
def get_class_options(
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
