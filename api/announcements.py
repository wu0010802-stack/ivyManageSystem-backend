"""
Announcements router - Admin CRUD for announcements
"""

import logging
from html.parser import HTMLParser
from typing import Literal, Optional, List
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from utils.errors import raise_safe_500
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import joinedload, selectinload

from models.database import (
    get_session,
    Announcement,
    AnnouncementParentRecipient,
    AnnouncementRead,
    AnnouncementRecipient,
    Classroom,
    Employee,
    Guardian,
    Student,
)
from utils.auth import require_staff_permission
from utils.error_messages import ANNOUNCEMENT_NOT_FOUND
from utils.permissions import Permission
from utils.portfolio_access import (
    accessible_classroom_ids,
    is_unrestricted,
)

from schemas._common import DeleteResultOut, MutationResultOut
from schemas.announcements import (
    AnnouncementListOut,
    AnnouncementParentRecipientsOut,
    AnnouncementReadersOut,
    AnnouncementRecipientsOut,
)


class _TagStripper(HTMLParser):
    """HTMLParser subclass that discards all tags and keeps only text nodes.

    `convert_charrefs=False` is intentional: it prevents entity-encoded
    payloads (e.g. ``&lt;img onerror=…&gt;``) from being decoded into real
    ``<`` / ``>`` characters before tag-stripping, which would let them bypass
    the filter and be stored as raw HTML in the database.
    Named entities and character references are re-emitted verbatim so that
    legitimate content like ``&amp;`` or ``&copy;`` is preserved.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def handle_entityref(self, name: str) -> None:
        # Preserve named HTML entities (e.g. &lt; stays as &lt;)
        self._chunks.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        # Preserve numeric character references (e.g. &#60; stays as &#60;)
        self._chunks.append(f"&#{name};")

    def get_text(self) -> str:
        return "".join(self._chunks)


def _strip_html(text: str) -> str:
    """Strip all HTML tags from *text*, returning plain-text content only.

    HTML entities such as ``&lt;`` are intentionally preserved (not decoded)
    so they remain safe regardless of how the stored value is later rendered.
    """
    if not text:
        return text
    p = _TagStripper()
    p.feed(text)
    p.close()
    return p.get_text()


def _validate_schedule(publish_at, expires_at) -> None:
    """expires_at 必須晚於 publish_at；publish_at 不可早於 now-5min。"""
    from utils.taipei_time import now_taipei_naive

    if publish_at is not None and expires_at is not None:
        if expires_at <= publish_at:
            raise HTTPException(
                status_code=400, detail="到期時間必須晚於發佈時間"
            )
    if publish_at is not None:
        threshold = now_taipei_naive() - timedelta(minutes=5)
        if publish_at < threshold:
            raise HTTPException(
                status_code=400, detail="排程發佈時間不可早於目前時間"
            )


logger = logging.getLogger(__name__)

_ANNOUNCEMENT_ALLOWED_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".pdf",
}
_ANNOUNCEMENT_ATTACHMENT_LIMIT = 5

router = APIRouter(prefix="/api/announcements", tags=["announcements"])


# ============ Pydantic Models ============


class AnnouncementCreate(BaseModel):
    title: str
    content: str
    priority: str = "normal"
    is_pinned: bool = False
    target_employee_ids: Optional[List[int]] = None  # None / [] = 全員可見
    publish_at: Optional[datetime] = None  # None = 立即發佈
    expires_at: Optional[datetime] = None  # None = 永不過期


class AnnouncementUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    priority: Optional[str] = None
    is_pinned: Optional[bool] = None
    target_employee_ids: Optional[List[int]] = None  # None = 不變；[] = 改為全員可見
    publish_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


# ============ Endpoints ============


@router.get("", response_model=AnnouncementListOut)
def list_announcements(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_READ)
    ),
):
    """列出所有公告（管理員用）。

    read_count / recipient_count 走 SQL correlated COUNT subquery；
    read_preview 走 batch query + Python group top-3（per announcement by read_at DESC）。
    完整 readers / recipient_ids 改為 lazy 端點 GET /announcements/{id}/readers 與
    GET /announcements/{id}/recipients，避免 list 路徑線性退化。
    """
    from sqlalchemy import func, select

    from services.announcements.visibility import derive_status
    from utils.taipei_time import now_taipei_naive

    _now = now_taipei_naive()
    session = get_session()
    try:
        read_count_subq = (
            select(func.count(AnnouncementRead.id))
            .where(AnnouncementRead.announcement_id == Announcement.id)
            .correlate(Announcement)
            .scalar_subquery()
        )
        recipient_count_subq = (
            select(func.count(AnnouncementRecipient.id))
            .where(AnnouncementRecipient.announcement_id == Announcement.id)
            .correlate(Announcement)
            .scalar_subquery()
        )

        query = (
            session.query(
                Announcement,
                read_count_subq.label("read_count"),
                recipient_count_subq.label("recipient_count"),
            )
            .options(joinedload(Announcement.author))
            .order_by(
                Announcement.is_pinned.desc(),
                Announcement.created_at.desc(),
            )
        )
        total = session.query(func.count(Announcement.id)).scalar() or 0
        rows = query.offset((page - 1) * page_size).limit(page_size).all()

        ann_ids = [ann.id for ann, *_ in rows]
        preview_map: dict[int, list[dict]] = {}
        if ann_ids:
            preview_rows = (
                session.query(
                    AnnouncementRead.announcement_id,
                    Employee.id,
                    Employee.name,
                    AnnouncementRead.read_at,
                )
                .join(Employee, Employee.id == AnnouncementRead.employee_id)
                .filter(AnnouncementRead.announcement_id.in_(ann_ids))
                .order_by(
                    AnnouncementRead.announcement_id,
                    AnnouncementRead.read_at.desc(),
                )
                .all()
            )
            for ann_id, emp_id, emp_name, read_at in preview_rows:
                bucket = preview_map.setdefault(ann_id, [])
                if len(bucket) < 3:
                    bucket.append(
                        {
                            "employee_id": emp_id,
                            "name": emp_name,
                            "read_at": read_at.isoformat() if read_at else None,
                        }
                    )

        results = []
        for ann, read_count, recipient_count in rows:
            preview = preview_map.get(ann.id, [])
            results.append(
                {
                    "id": ann.id,
                    "title": ann.title,
                    "content": ann.content,
                    "priority": ann.priority,
                    "is_pinned": ann.is_pinned,
                    "created_by": ann.created_by,
                    "created_by_name": ann.author.name if ann.author else "未知",
                    "created_at": (
                        ann.created_at.isoformat() if ann.created_at else None
                    ),
                    "updated_at": (
                        ann.updated_at.isoformat() if ann.updated_at else None
                    ),
                    "publish_at": ann.publish_at.isoformat() if ann.publish_at else None,
                    "expires_at": ann.expires_at.isoformat() if ann.expires_at else None,
                    "status": derive_status(ann, _now),
                    "read_count": int(read_count or 0),
                    "read_preview": preview,
                    "has_more_readers": int(read_count or 0) > len(preview),
                    "recipient_count": int(recipient_count or 0),
                }
            )
        return {"total": int(total), "items": results}
    finally:
        session.close()


@router.post("", status_code=201, response_model=MutationResultOut)
def create_announcement(
    data: AnnouncementCreate,
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_WRITE)
    ),
):
    """新增公告"""
    if data.priority not in ("normal", "important", "urgent"):
        raise HTTPException(status_code=400, detail="無效的優先級")
    _validate_schedule(data.publish_at, data.expires_at)

    session = get_session()
    try:
        ann = Announcement(
            title=_strip_html(data.title),
            content=_strip_html(data.content),
            priority=data.priority,
            is_pinned=data.is_pinned,
            publish_at=data.publish_at,
            expires_at=data.expires_at,
            created_by=current_user["employee_id"],
        )
        session.add(ann)
        session.flush()  # 取得 ann.id

        if data.target_employee_ids:
            for emp_id in data.target_employee_ids:
                session.add(
                    AnnouncementRecipient(announcement_id=ann.id, employee_id=emp_id)
                )

        session.commit()
        return {"message": "公告已發佈", "id": ann.id}
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.put("/{announcement_id}", response_model=DeleteResultOut)
def update_announcement(
    announcement_id: int,
    data: AnnouncementUpdate,
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_WRITE)
    ),
):
    """更新公告"""
    session = get_session()
    try:
        ann = (
            session.query(Announcement)
            .filter(Announcement.id == announcement_id)
            .first()
        )
        if not ann:
            raise HTTPException(status_code=404, detail=ANNOUNCEMENT_NOT_FOUND)

        if data.title is not None:
            ann.title = _strip_html(data.title)
        if data.content is not None:
            ann.content = _strip_html(data.content)
        if data.priority is not None:
            if data.priority not in ("normal", "important", "urgent"):
                raise HTTPException(status_code=400, detail="無效的優先級")
            ann.priority = data.priority
        if data.is_pinned is not None:
            ann.is_pinned = data.is_pinned

        new_publish = data.publish_at if data.publish_at is not None else ann.publish_at
        new_expires = data.expires_at if data.expires_at is not None else ann.expires_at
        _validate_schedule(new_publish, new_expires)
        ann.publish_at = new_publish
        ann.expires_at = new_expires

        if data.target_employee_ids is not None:
            # 清除舊 recipients，再批次 INSERT 新的
            session.query(AnnouncementRecipient).filter(
                AnnouncementRecipient.announcement_id == announcement_id
            ).delete()
            for emp_id in data.target_employee_ids:
                session.add(
                    AnnouncementRecipient(
                        announcement_id=announcement_id, employee_id=emp_id
                    )
                )

        session.commit()
        return {"message": "公告已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/{announcement_id}", response_model=DeleteResultOut)
def delete_announcement(
    announcement_id: int,
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_WRITE)
    ),
):
    """刪除公告"""
    session = get_session()
    try:
        ann = (
            session.query(Announcement)
            .filter(Announcement.id == announcement_id)
            .first()
        )
        if not ann:
            raise HTTPException(status_code=404, detail=ANNOUNCEMENT_NOT_FOUND)

        session.delete(ann)
        session.commit()
        return {"message": "公告已刪除"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get(
    "/{announcement_id}/recipients",
    response_model=AnnouncementRecipientsOut,
)
def list_recipients(
    announcement_id: int,
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_READ)
    ),
):
    """Lazy fetch admin edit dialog 用的 recipient 員工 id 清單。"""
    session = get_session()
    try:
        ann = (
            session.query(Announcement.id)
            .filter(Announcement.id == announcement_id)
            .first()
        )
        if not ann:
            raise HTTPException(status_code=404, detail=ANNOUNCEMENT_NOT_FOUND)
        rows = (
            session.query(AnnouncementRecipient.employee_id)
            .filter(AnnouncementRecipient.announcement_id == announcement_id)
            .all()
        )
        return {"employee_ids": [r[0] for r in rows]}
    finally:
        session.close()


@router.get(
    "/{announcement_id}/readers",
    response_model=AnnouncementReadersOut,
)
def list_readers(
    announcement_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_READ)
    ),
):
    """Lazy fetch admin popover 用的完整已讀名單（分頁、read_at DESC）。"""
    from sqlalchemy import func

    session = get_session()
    try:
        ann = (
            session.query(Announcement.id)
            .filter(Announcement.id == announcement_id)
            .first()
        )
        if not ann:
            raise HTTPException(status_code=404, detail=ANNOUNCEMENT_NOT_FOUND)
        total = (
            session.query(func.count(AnnouncementRead.id))
            .filter(AnnouncementRead.announcement_id == announcement_id)
            .scalar()
            or 0
        )
        rows = (
            session.query(AnnouncementRead, Employee.name)
            .join(Employee, Employee.id == AnnouncementRead.employee_id)
            .filter(AnnouncementRead.announcement_id == announcement_id)
            .order_by(AnnouncementRead.read_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        items = [
            {
                "employee_id": r.employee_id,
                "name": name,
                "read_at": r.read_at.isoformat() if r.read_at else None,
            }
            for r, name in rows
        ]
        return {
            "items": items,
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }
    finally:
        session.close()


@router.post("/{announcement_id}/attachments", status_code=201)
async def upload_announcement_attachment(
    announcement_id: int,
    file: UploadFile = File(...),
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_WRITE)
    ),
) -> dict:
    """上傳公告附件（圖片 / PDF），單一公告最多 5 個。"""
    import os

    from models.database import Attachment, session_scope
    from models.portfolio import ATTACHMENT_OWNER_ANNOUNCEMENT
    from utils.file_upload import (
        read_upload_with_size_check,
        safe_attachment_filename,
        validate_file_signature,
    )
    from utils.portfolio_storage import get_portfolio_storage

    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ANNOUNCEMENT_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的檔案格式：{ext or '未知'}；僅接受 JPG/PNG/GIF/HEIC/PDF",
        )

    with session_scope() as session:
        ann = (
            session.query(Announcement)
            .filter(Announcement.id == announcement_id)
            .first()
        )
        if not ann:
            raise HTTPException(status_code=404, detail=ANNOUNCEMENT_NOT_FOUND)
        existing_count = (
            session.query(Attachment)
            .filter(
                Attachment.owner_type == ATTACHMENT_OWNER_ANNOUNCEMENT,
                Attachment.owner_id == announcement_id,
                Attachment.deleted_at.is_(None),
            )
            .count()
        )
        if existing_count >= _ANNOUNCEMENT_ATTACHMENT_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=f"附件上限 {_ANNOUNCEMENT_ATTACHMENT_LIMIT} 個",
            )

    content = await read_upload_with_size_check(file, extension=ext)
    # read_upload_with_size_check already calls validate_file_signature internally
    # when extension is provided; calling again is idempotent and safe.
    validate_file_signature(content, ext)

    storage = get_portfolio_storage()
    stored = storage.put_attachment(content, ext)

    with session_scope() as session:
        att = Attachment(
            owner_type=ATTACHMENT_OWNER_ANNOUNCEMENT,
            owner_id=announcement_id,
            storage_key=stored.storage_key,
            display_key=stored.display_key,
            thumb_key=stored.thumb_key,
            original_filename=safe_attachment_filename(filename, ext),
            mime_type=stored.mime_type,
            size_bytes=len(content),
            uploaded_by=current_user.get("user_id"),
        )
        session.add(att)
        session.flush()
        return _serialize_attachment_for_announcement(att)


def _serialize_attachment_for_announcement(att) -> dict:
    """公告 attachment 序列化（與 list 端統一）。"""
    from utils.portfolio_storage import PORTFOLIO_MODULE

    return {
        "id": att.id,
        "filename": att.original_filename,
        "mime_type": att.mime_type,
        "size_bytes": att.size_bytes,
        "url": f"/api/uploads/{PORTFOLIO_MODULE}/{att.storage_key}",
        "thumb_url": (
            f"/api/uploads/{PORTFOLIO_MODULE}/{att.thumb_key}"
            if att.thumb_key
            else None
        ),
    }


@router.delete(
    "/{announcement_id}/attachments/{attachment_id}",
    response_model=DeleteResultOut,
)
def delete_announcement_attachment(
    announcement_id: int,
    attachment_id: int,
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_WRITE)
    ),
):
    """軟刪除公告附件。實檔保留 90 天由清理 job 接手。"""
    from models.database import Attachment, session_scope
    from models.portfolio import ATTACHMENT_OWNER_ANNOUNCEMENT
    from utils.taipei_time import now_taipei_naive

    with session_scope() as session:
        att = (
            session.query(Attachment)
            .filter(
                Attachment.id == attachment_id,
                Attachment.owner_type == ATTACHMENT_OWNER_ANNOUNCEMENT,
                Attachment.owner_id == announcement_id,
                Attachment.deleted_at.is_(None),
            )
            .first()
        )
        if not att:
            raise HTTPException(status_code=404, detail="附件不存在")
        att.deleted_at = now_taipei_naive()
    return {"message": "附件已刪除"}


# ============ 家長端 scope（plan A.5） ============
#
# 員工端 announcement_recipients 不動；對家長另寫 announcement_parent_recipients。
# 一筆 announcement 可同時對員工與家長兩端都有發送對象。
#
# scope 規則：
# - 'all'       → 對所有家長可見（其他欄位必為 None）
# - 'classroom' → 僅該班學生的家長可見（classroom_id 必填）
# - 'student'   → 僅該學生的家長可見（student_id 必填）
# - 'guardian'  → 僅該監護人本人可見（guardian_id 必填）
#
# 寫入採 replace-all 語意：PUT 時清掉舊的、寫入新的。


class ParentRecipientItem(BaseModel):
    scope: Literal["all", "classroom", "student", "guardian"]
    classroom_id: Optional[int] = Field(None, gt=0)
    student_id: Optional[int] = Field(None, gt=0)
    guardian_id: Optional[int] = Field(None, gt=0)

    @model_validator(mode="after")
    def _check_scope_and_id(self):
        scope_to_field = {
            "all": None,
            "classroom": "classroom_id",
            "student": "student_id",
            "guardian": "guardian_id",
        }
        required_field = scope_to_field[self.scope]
        for f in ("classroom_id", "student_id", "guardian_id"):
            value = getattr(self, f)
            if f == required_field:
                if value is None:
                    raise ValueError(f"scope='{self.scope}' 必須提供 {f}")
            else:
                if value is not None:
                    raise ValueError(
                        f"scope='{self.scope}' 不可帶 {f}（僅在對應 scope 下才有意義）"
                    )
        return self


class ParentRecipientsUpdate(BaseModel):
    recipients: List[ParentRecipientItem]


def _serialize_parent_recipient(r: AnnouncementParentRecipient) -> dict:
    return {
        "id": r.id,
        "scope": r.scope,
        "classroom_id": r.classroom_id,
        "student_id": r.student_id,
        "guardian_id": r.guardian_id,
    }


@router.get("/{announcement_id}/parent-recipients", response_model=AnnouncementParentRecipientsOut)
def list_parent_recipients(
    announcement_id: int,
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_READ)
    ),
):
    """讀取目前公告對家長的發送對象設定。"""
    session = get_session()
    try:
        ann = (
            session.query(Announcement)
            .filter(Announcement.id == announcement_id)
            .first()
        )
        if not ann:
            raise HTTPException(status_code=404, detail=ANNOUNCEMENT_NOT_FOUND)
        rows = (
            session.query(AnnouncementParentRecipient)
            .filter(AnnouncementParentRecipient.announcement_id == announcement_id)
            .order_by(AnnouncementParentRecipient.id.asc())
            .all()
        )
        return {
            "announcement_id": announcement_id,
            "items": [_serialize_parent_recipient(r) for r in rows],
            "total": len(rows),
        }
    finally:
        session.close()


def _validate_recipient_targets_exist(
    session, recipients: list[ParentRecipientItem]
) -> None:
    """確認 classroom/student/guardian id 都存在；不存在則 400。"""
    classroom_ids = {r.classroom_id for r in recipients if r.scope == "classroom"}
    student_ids = {r.student_id for r in recipients if r.scope == "student"}
    guardian_ids = {r.guardian_id for r in recipients if r.scope == "guardian"}

    if classroom_ids:
        existing = {
            cid
            for (cid,) in session.query(Classroom.id).filter(
                Classroom.id.in_(classroom_ids)
            )
        }
        missing = classroom_ids - existing
        if missing:
            raise HTTPException(
                status_code=400, detail=f"找不到班級 id={sorted(missing)}"
            )
    if student_ids:
        existing = {
            sid
            for (sid,) in session.query(Student.id).filter(Student.id.in_(student_ids))
        }
        missing = student_ids - existing
        if missing:
            raise HTTPException(
                status_code=400, detail=f"找不到學生 id={sorted(missing)}"
            )
    if guardian_ids:
        existing = {
            gid
            for (gid,) in session.query(Guardian.id).filter(
                Guardian.id.in_(guardian_ids), Guardian.deleted_at.is_(None)
            )
        }
        missing = guardian_ids - existing
        if missing:
            raise HTTPException(
                status_code=400, detail=f"找不到監護人 id={sorted(missing)}"
            )


def _validate_recipient_audience_scope(
    session, recipients: list[ParentRecipientItem], current_user: dict
) -> None:
    """F-045：受眾範圍守衛——非 admin/hr/supervisor 僅能對自己班的學生/家長發訊。

    ANNOUNCEMENTS_WRITE 在預設配置下只給 admin/supervisor，但業主可自訂角色把這個
    bit 授給「公關/行政助理」。一旦這類非 unrestricted caller 持有此權限，就能
    透過 scope=guardian/student/classroom + 任意 id 對任何家長發訊（社交工程攻擊面）。
    本 helper 對非 unrestricted caller 強制：
      - scope='all'：拒絕（會打到全校家長，僅 admin/supervisor 可用）
      - scope='classroom' / 'student' / 'guardian'：必須落在 caller 的
        accessible_classroom_ids 範圍內

    admin / hr / supervisor（is_unrestricted）不受此限制，與其他 admin 工具一致。
    """
    if is_unrestricted(current_user):
        return

    allowed_classrooms = set(accessible_classroom_ids(session, current_user))

    # scope='all' 會繞過受眾範圍；非 unrestricted caller 一律拒
    if any(r.scope == "all" for r in recipients):
        raise HTTPException(
            status_code=403,
            detail="僅 admin/supervisor 可對全校家長發送公告",
        )

    classroom_ids = {r.classroom_id for r in recipients if r.scope == "classroom"}
    out_of_scope_classrooms = classroom_ids - allowed_classrooms
    if out_of_scope_classrooms:
        raise HTTPException(
            status_code=403,
            detail="無權對非自己班級的家長發送公告",
        )

    student_ids = {r.student_id for r in recipients if r.scope == "student"}
    if student_ids:
        rows = (
            session.query(Student.id, Student.classroom_id)
            .filter(Student.id.in_(student_ids))
            .all()
        )
        for sid, cid in rows:
            if cid is None or cid not in allowed_classrooms:
                raise HTTPException(
                    status_code=403,
                    detail="無權對非自己班級的學生家長發送公告",
                )

    guardian_ids = {r.guardian_id for r in recipients if r.scope == "guardian"}
    if guardian_ids:
        rows = (
            session.query(Guardian.id, Student.classroom_id)
            .join(Student, Student.id == Guardian.student_id)
            .filter(
                Guardian.id.in_(guardian_ids),
                Guardian.deleted_at.is_(None),
            )
            .all()
        )
        for gid, cid in rows:
            if cid is None or cid not in allowed_classrooms:
                raise HTTPException(
                    status_code=403,
                    detail="無權對非自己班級的家長發送公告",
                )


# ── 家長公告推播：caller 走 services.notification.dispatch.enqueue（PR-B）──


def _resolve_parent_user_ids(
    session, recipients: list[AnnouncementParentRecipient]
) -> set[int]:
    """依 recipient scope 反查所有應收到推播的 parent user_id 集合。"""
    from models.database import Classroom, Guardian, Student, User

    user_ids: set[int] = set()
    has_all = any(r.scope == "all" for r in recipients)
    if has_all:
        # 'all' = 全體家長 user；LINE 可達性過濾統一交給 should_push_to_parent gate
        rows = (
            session.query(User.id)
            .filter(
                User.role == "parent",
                User.is_active == True,  # noqa: E712
            )
            .all()
        )
        for r in rows:
            user_ids.add(r[0])
        return user_ids

    classroom_ids = {r.classroom_id for r in recipients if r.scope == "classroom"}
    student_ids = {r.student_id for r in recipients if r.scope == "student"}
    guardian_ids = {r.guardian_id for r in recipients if r.scope == "guardian"}

    if classroom_ids:
        rows = (
            session.query(Guardian.user_id)
            .join(Student, Student.id == Guardian.student_id)
            .filter(
                Student.classroom_id.in_(classroom_ids),
                Guardian.user_id.isnot(None),
                Guardian.deleted_at.is_(None),
            )
            .all()
        )
        for r in rows:
            user_ids.add(r[0])

    if student_ids:
        rows = (
            session.query(Guardian.user_id)
            .filter(
                Guardian.student_id.in_(student_ids),
                Guardian.user_id.isnot(None),
                Guardian.deleted_at.is_(None),
            )
            .all()
        )
        for r in rows:
            user_ids.add(r[0])

    if guardian_ids:
        rows = (
            session.query(Guardian.user_id)
            .filter(
                Guardian.id.in_(guardian_ids),
                Guardian.user_id.isnot(None),
                Guardian.deleted_at.is_(None),
            )
            .all()
        )
        for r in rows:
            user_ids.add(r[0])

    return user_ids


def _fire_announcement_push(
    session,
    announcement: Announcement,
    recipients: list[AnnouncementParentRecipient],
    *,
    sender_user_id: Optional[int] = None,
) -> None:
    """對所有應收到此公告的家長 enqueue parent.announcement 通知。

    必須在 session.commit() 之前呼叫；dispatch 在 after_commit hook 內 fan-out。
    LINE 可達性（active + line_user_id + line_follow_confirmed_at）與通知偏好
    gate 由 dispatch 內部統一處理，caller 不再篩 user_ids。
    """
    from services.notification import dispatch

    user_ids = _resolve_parent_user_ids(session, recipients)
    for uid in user_ids:
        dispatch.enqueue(
            session=session,
            event_type="parent.announcement",
            recipient_user_id=uid,
            context={
                "title": announcement.title,
                "preview": announcement.content,
                "announcement_id": announcement.id,
            },
            sender_id=sender_user_id,
            source_entity_type="announcement",
            source_entity_id=announcement.id,
        )
    logger.info(
        "公告通知 enqueue：announcement_id=%s 對 %d 名家長",
        announcement.id,
        len(user_ids),
    )


@router.put("/{announcement_id}/parent-recipients", response_model=AnnouncementParentRecipientsOut)
def replace_parent_recipients(
    announcement_id: int,
    payload: ParentRecipientsUpdate,
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_WRITE)
    ),
):
    """整批替換公告對家長的發送對象。

    - 空清單 = 對家長端不顯示（家長 portal 看不到此公告）
    - 含 scope='all' 即可讓所有家長看到（其他項目可同時並存，但 'all'
      已涵蓋；前端不應同時送 'all' + 其他 scope，後端不強擋以保留彈性）
    """
    return _replace_recipients_impl(
        announcement_id, payload.recipients or [], current_user
    )


def _replace_recipients_impl(
    announcement_id: int,
    recipients: list[ParentRecipientItem],
    current_user: dict,
) -> dict:
    session = get_session()
    try:
        ann = (
            session.query(Announcement)
            .filter(Announcement.id == announcement_id)
            .first()
        )
        if not ann:
            raise HTTPException(status_code=404, detail=ANNOUNCEMENT_NOT_FOUND)

        _validate_recipient_targets_exist(session, recipients)
        # F-045：受眾範圍守衛（非 admin/hr/supervisor 僅能對自己班發訊）
        _validate_recipient_audience_scope(session, recipients, current_user)

        # replace-all：先清舊
        session.query(AnnouncementParentRecipient).filter(
            AnnouncementParentRecipient.announcement_id == announcement_id
        ).delete(synchronize_session=False)

        for item in recipients:
            session.add(
                AnnouncementParentRecipient(
                    announcement_id=announcement_id,
                    scope=item.scope,
                    classroom_id=item.classroom_id,
                    student_id=item.student_id,
                    guardian_id=item.guardian_id,
                )
            )
        # flush 取得 AnnouncementParentRecipient.id（_resolve_parent_user_ids 用得到）
        session.flush()

        rows = (
            session.query(AnnouncementParentRecipient)
            .filter(AnnouncementParentRecipient.announcement_id == announcement_id)
            .order_by(AnnouncementParentRecipient.id.asc())
            .all()
        )
        logger.warning(
            "[announcement-parent-recipients] announcement_id=%s 重設對家長 scope，共 %d 項",
            announcement_id,
            len(rows),
        )

        # 通知 enqueue（commit 前；dispatch after_commit hook 自動 fan-out）
        if rows:
            from utils.taipei_time import now_taipei_naive as _now_tn
            if ann.publish_at is not None and ann.publish_at > _now_tn():
                logger.info(
                    "announcement %s publish_at 未到（%s），跳過立即推播；scheduler 接手",
                    announcement_id,
                    ann.publish_at.isoformat(),
                )
            else:
                try:
                    _fire_announcement_push(
                        session,
                        ann,
                        rows,
                        sender_user_id=current_user.get("user_id"),
                    )
                except Exception as exc:
                    logger.warning(
                        "announcement enqueue 失敗（已吞）：announcement_id=%s err=%s",
                        announcement_id,
                        exc,
                    )

        session.commit()
        return {
            "announcement_id": announcement_id,
            "items": [_serialize_parent_recipient(r) for r in rows],
            "total": len(rows),
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
