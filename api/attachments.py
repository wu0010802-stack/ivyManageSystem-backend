"""
Attachments router — 多型附件上傳 / 下載 / 軟刪除

路由：
- POST   /api/attachments              multipart upload（含縮圖生成），回傳 id + 三組 URL
- DELETE /api/attachments/{id}         軟刪除（實際檔案保留 90 天由清理 job 處理）
- GET    /uploads/portfolio/{key:path} 授權檔案下載（auth + owner 反查 + 班級 scope）

安全注意：
- /uploads/portfolio 路由**絕不**用 FastAPI StaticFiles 直接 mount，必經此 handler 的權限檢查
- key 經 `storage.absolute_path()` 做 path traversal 防護
- magic bytes 驗證後才落盤（影像/影片白名單）
"""

from __future__ import annotations

import logging
import os
from typing import Literal, Optional

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse

from models.database import (
    Attachment,
    Student,
    StudentObservation,
    session_scope,
)
from models.portfolio import ATTACHMENT_OWNER_TYPES, ATTACHMENT_OWNER_OBSERVATION
from utils.auth import require_permission
from utils.errors import raise_safe_500
from utils.file_upload import (
    max_upload_size_for,
    read_upload_with_size_check,
    validate_file_signature,
)
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access
from utils.portfolio_storage import (
    PORTFOLIO_MODULE,
    get_portfolio_storage,
    heic_supported,
    is_heic_extension,
    is_image_extension,
    is_video_extension,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["attachments"])

# 下載路由掛在 /api 之下，才能共用既有 access_token cookie（path="/api"）
# 否則瀏覽器不會把 cookie 帶進 /uploads/... 的 request。
download_router = APIRouter(prefix="/api/uploads", tags=["attachments-download"])


# ── 允許的副檔名白名單（Portfolio 範疇） ─────────────────────────────────
_PORTFOLIO_ALLOWED_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".heic",
    ".heif",
    ".mp4",
    ".mov",
    ".webm",
}


# ── Helpers ──────────────────────────────────────────────────────────────


def _url_for_key(key: str) -> str:
    """將 storage_key 轉為對外 URL（/api 前綴以共用 auth cookie）。"""
    return f"/api/uploads/{PORTFOLIO_MODULE}/{key}"


def _extension_of(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _resolve_owner_student_id(session, owner_type: str, owner_id: int) -> int:
    """依 owner_type 反查對應的 student_id，用於權限檢查。

    Batch A 僅支援 owner_type='observation'；後續 batches 新增時補上。
    """
    if owner_type == ATTACHMENT_OWNER_OBSERVATION:
        obs = (
            session.query(StudentObservation)
            .filter(StudentObservation.id == owner_id)
            .first()
        )
        if not obs:
            raise HTTPException(status_code=404, detail="對應的觀察紀錄不存在")
        return obs.student_id

    # Batch B/C 會擴充（report / medication_order）。目前傳進來就 reject
    raise HTTPException(
        status_code=400,
        detail=f"不支援的 owner_type：{owner_type}",
    )


def _attachment_to_dict(att: Attachment) -> dict:
    return {
        "id": att.id,
        "owner_type": att.owner_type,
        "owner_id": att.owner_id,
        "original_filename": att.original_filename,
        "mime_type": att.mime_type,
        "size_bytes": att.size_bytes,
        "url": _url_for_key(att.storage_key),
        "display_url": _url_for_key(att.display_key) if att.display_key else None,
        "thumb_url": _url_for_key(att.thumb_key) if att.thumb_key else None,
        "uploaded_by": att.uploaded_by,
        "created_at": att.created_at.isoformat() if att.created_at else None,
    }


# ── Routes ───────────────────────────────────────────────────────────────


@router.post("/attachments", status_code=201)
async def upload_attachment(
    request: Request,
    file: UploadFile,
    owner_type: Literal["observation", "report", "medication_order"] = Form(...),
    owner_id: int = Form(..., ge=1),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
) -> dict:
    """上傳附件並自動生成 display / thumb 變體（影像）。"""
    if owner_type not in ATTACHMENT_OWNER_TYPES:
        raise HTTPException(status_code=400, detail="owner_type 不合法")

    filename = file.filename or ""
    ext = _extension_of(filename)
    if ext not in _PORTFOLIO_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的檔案格式：{ext or '未知'}；僅接受 JPG/PNG/GIF/HEIC/MP4/MOV/WEBM",
        )

    # HEIC 需額外檢查 pillow-heif 是否已載入
    if is_heic_extension(ext) and not heic_supported():
        raise HTTPException(
            status_code=400,
            detail="伺服器未安裝 HEIC 解碼套件，請將照片轉成 JPG/PNG 後再上傳",
        )

    # 讀檔 + size check（影片 50MB、其他 10MB）+ magic bytes 驗證
    content = await read_upload_with_size_check(file, extension=ext)
    validate_file_signature(content, ext)

    try:
        with session_scope() as session:
            # 反查 owner 的 student_id + 班級 scope 檢查
            student_id = _resolve_owner_student_id(session, owner_type, owner_id)
            assert_student_access(session, current_user, student_id)

            storage = get_portfolio_storage()
            stored = storage.put_attachment(content, ext)

            att = Attachment(
                owner_type=owner_type,
                owner_id=owner_id,
                storage_key=stored.storage_key,
                display_key=stored.display_key,
                thumb_key=stored.thumb_key,
                original_filename=filename,
                mime_type=stored.mime_type,
                size_bytes=len(content),
                uploaded_by=current_user.get("user_id"),
            )
            session.add(att)
            session.flush()
            session.refresh(att)

            # Audit：把 entity_id 指向學生（方便以學生維度查操作軌跡）
            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"上傳 {owner_type} 附件：student_id={student_id}, "
                f"filename={filename}, size={len(content)}B"
            )

            logger.info(
                "上傳附件：owner=%s#%d student_id=%d filename=%s size=%d operator=%s",
                owner_type,
                owner_id,
                student_id,
                filename,
                len(content),
                current_user.get("username"),
            )
            return _attachment_to_dict(att)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="上傳附件失敗")


@router.delete("/attachments/{attachment_id}")
async def delete_attachment(
    attachment_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
) -> dict:
    """軟刪除附件。實際檔案保留 90 天後由清理 job 處理。"""
    from datetime import datetime

    try:
        with session_scope() as session:
            att = (
                session.query(Attachment).filter(Attachment.id == attachment_id).first()
            )
            if not att:
                raise HTTPException(status_code=404, detail="附件不存在")
            if att.deleted_at:
                return {"message": "附件已刪除"}

            # 反查並檢查權限
            student_id = _resolve_owner_student_id(
                session, att.owner_type, att.owner_id
            )
            assert_student_access(session, current_user, student_id)

            att.deleted_at = datetime.now()
            session.flush()

            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"軟刪除附件：attachment_id={attachment_id}, "
                f"owner={att.owner_type}#{att.owner_id}"
            )
            logger.info(
                "軟刪除附件：id=%d owner=%s#%d student_id=%d operator=%s",
                attachment_id,
                att.owner_type,
                att.owner_id,
                student_id,
                current_user.get("username"),
            )
            return {"message": "刪除成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="刪除附件失敗")


# ── 檔案下載（帶權限守衛） ────────────────────────────────────────────────


@download_router.get("/portfolio/{key:path}")
async def download_portfolio_file(
    key: str,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
) -> Response:
    """讀取附件檔案內容。

    流程：
    1. 依 key 反查 attachments（同時匹配 storage_key / display_key / thumb_key）
    2. 檢查軟刪除
    3. 反查 owner 的 student_id，做班級 scope
    4. 回傳檔案
    """
    try:
        with session_scope() as session:
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

            # 權限：反查 student + 班級 scope
            student_id = _resolve_owner_student_id(
                session, att.owner_type, att.owner_id
            )
            assert_student_access(session, current_user, student_id)

            storage = get_portfolio_storage()
            path = storage.absolute_path(key)
            if not path.exists():
                logger.error("附件實體檔案遺失：key=%s attachment_id=%d", key, att.id)
                raise HTTPException(status_code=404, detail="檔案實體遺失")

            mime = (
                att.mime_type
                if key == att.storage_key
                else "image/jpeg"  # display/thumb 固定 JPG
            )
            return FileResponse(
                path=str(path),
                media_type=mime,
                filename=att.original_filename if key == att.storage_key else None,
            )
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="讀取附件失敗")
