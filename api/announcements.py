"""
Announcements router - Admin CRUD for announcements
"""

import logging
from html.parser import HTMLParser
from typing import Literal, Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import joinedload, selectinload

from models.database import (
    get_session,
    Announcement,
    AnnouncementParentRecipient,
    AnnouncementRecipient,
    Classroom,
    Employee,
    Guardian,
    Student,
)
from utils.auth import require_staff_permission
from utils.error_messages import ANNOUNCEMENT_NOT_FOUND
from utils.permissions import Permission


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


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/announcements", tags=["announcements"])


# ============ Pydantic Models ============


class AnnouncementCreate(BaseModel):
    title: str
    content: str
    priority: str = "normal"
    is_pinned: bool = False
    target_employee_ids: Optional[List[int]] = None  # None / [] = 全員可見


class AnnouncementUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    priority: Optional[str] = None
    is_pinned: Optional[bool] = None
    target_employee_ids: Optional[List[int]] = None  # None = 不變；[] = 改為全員可見


# ============ Endpoints ============


@router.get("")
def list_announcements(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_READ)
    ),
):
    """列出所有公告（管理員用）"""
    session = get_session()
    try:
        query = (
            session.query(Announcement)
            .options(
                joinedload(Announcement.author),
                selectinload(Announcement.reads),
                selectinload(Announcement.recipients),
            )
            .order_by(
                Announcement.is_pinned.desc(),
                Announcement.created_at.desc(),
            )
        )
        total = query.count()
        items = query.offset((page - 1) * page_size).limit(page_size).all()

        read_employee_ids = {
            read.employee_id
            for ann in items
            for read in ann.reads
            if read.employee_id is not None
        }
        read_employee_map = {}
        if read_employee_ids:
            employees = (
                session.query(Employee.id, Employee.name)
                .filter(Employee.id.in_(read_employee_ids))
                .all()
            )
            read_employee_map = {employee.id: employee.name for employee in employees}

        results = []
        for ann in items:
            recipient_ids = [r.employee_id for r in ann.recipients]
            sorted_reads = sorted(
                ann.reads,
                key=lambda read: read.read_at or 0,
                reverse=True,
            )
            readers = [
                {
                    "employee_id": read.employee_id,
                    "name": read_employee_map.get(read.employee_id, "未知"),
                    "read_at": read.read_at.isoformat() if read.read_at else None,
                }
                for read in sorted_reads
            ]
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
                    "read_count": len(ann.reads),
                    "read_preview": readers[:3],
                    "has_more_readers": len(readers) > 3,
                    "readers": readers,
                    "recipient_count": len(recipient_ids),
                    "recipient_ids": recipient_ids,
                }
            )

        return {"total": total, "items": results}
    finally:
        session.close()


@router.post("", status_code=201)
def create_announcement(
    data: AnnouncementCreate,
    current_user: dict = Depends(
        require_staff_permission(Permission.ANNOUNCEMENTS_WRITE)
    ),
):
    """新增公告"""
    if data.priority not in ("normal", "important", "urgent"):
        raise HTTPException(status_code=400, detail="無效的優先級")

    session = get_session()
    try:
        ann = Announcement(
            title=_strip_html(data.title),
            content=_strip_html(data.content),
            priority=data.priority,
            is_pinned=data.is_pinned,
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


@router.put("/{announcement_id}")
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


@router.delete("/{announcement_id}")
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


@router.get("/{announcement_id}/parent-recipients")
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


# ── LINE 推播（Phase 4） ──────────────────────────────────────────────────

# 由 main.py 注入的 LineService singleton；未注入時 push 變 no-op
_line_service = None


def init_announcement_line_service(svc) -> None:
    global _line_service
    _line_service = svc


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
) -> None:
    """推播給所有應收到此公告的家長。每位家長走 should_push_to_parent gate。"""
    if _line_service is None:
        return
    user_ids = _resolve_parent_user_ids(session, recipients)
    sent = 0
    for uid in user_ids:
        line_id = _line_service.should_push_to_parent(
            session, user_id=uid, event_type="announcement"
        )
        if not line_id:
            continue
        # 直接呼叫既有 notify_parent_announcement（不會再做 gate；line_id 已驗）
        _line_service.notify_parent_announcement(
            line_id, announcement.title, announcement.content
        )
        sent += 1
    logger.info(
        "公告 LINE 推播：announcement_id=%s 對 %d 名家長推播",
        announcement.id,
        sent,
    )


@router.put("/{announcement_id}/parent-recipients")
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
    if not payload.recipients:
        return _replace_recipients_impl(announcement_id, [])
    return _replace_recipients_impl(announcement_id, payload.recipients)


def _replace_recipients_impl(
    announcement_id: int, recipients: list[ParentRecipientItem]
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
        session.commit()

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

        # Phase 4：推播 LINE 通知（fire-and-forget；commit 後執行；空 recipient 不推）
        if rows:
            try:
                _fire_announcement_push(session, ann, rows)
            except Exception as exc:
                logger.warning(
                    "announcement push 失敗（已吞）：announcement_id=%s err=%s",
                    announcement_id,
                    exc,
                )
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
