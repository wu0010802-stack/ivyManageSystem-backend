"""api/portal/contact_book.py — 教師端每日聯絡簿

端點：
- GET    /api/portal/contact-book?classroom_id=&log_date=     列班級當日（含草稿與發布）
- POST   /api/portal/contact-book/batch                       班級表式批次 upsert（草稿）
- PUT    /api/portal/contact-book/{id}                        單筆編輯（樂觀鎖 If-Match）
- POST   /api/portal/contact-book/{id}/publish                發布（觸發 WS + LINE）
- POST   /api/portal/contact-book/{id}/photos                 上傳照片
- DELETE /api/portal/contact-book/{id}/photos/{att_id}        軟刪照片
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field

from models.database import (
    Attachment,
    Classroom,
    Student,
    StudentContactBookEntry,
    get_session,
)
from models.contact_book import ContactBookTemplate
from models.portfolio import ATTACHMENT_OWNER_CONTACT_BOOK
from services.contact_book_service import (
    apply_template_fields,
    compute_class_completion,
    copy_yesterday_to_today,
    publish_entry,
)
from utils.auth import require_permission
from utils.file_upload import read_upload_with_size_check, validate_file_signature
from utils.permissions import Permission
from utils.portfolio_storage import heic_supported, is_heic_extension

from ._shared import _get_employee, _get_teacher_classroom_ids

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/contact-book", tags=["portal-contact-book"])

_ALLOWED_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif"}

# Service injection（main.py 啟動時注入單例 LineService）
_line_service = None


def init_contact_book_line_service(line_service) -> None:
    global _line_service
    _line_service = line_service


# ── Pydantic ──────────────────────────────────────────────────────────────


class ContactBookEntryFields(BaseModel):
    mood: Optional[str] = Field(default=None, max_length=20)
    meal_lunch: Optional[int] = Field(default=None, ge=0, le=3)
    meal_snack: Optional[int] = Field(default=None, ge=0, le=3)
    nap_minutes: Optional[int] = Field(default=None, ge=0, le=600)
    bowel: Optional[str] = Field(default=None, max_length=20)
    temperature_c: Optional[float] = Field(default=None, ge=30, le=45)
    teacher_note: Optional[str] = Field(default=None, max_length=2000)
    learning_highlight: Optional[str] = Field(default=None, max_length=2000)


class ContactBookBatchItem(ContactBookEntryFields):
    student_id: int = Field(..., gt=0)


class ContactBookBatchPayload(BaseModel):
    classroom_id: int = Field(..., gt=0)
    log_date: date
    items: list[ContactBookBatchItem] = Field(..., min_length=1, max_length=100)


class CopyYesterdayPayload(BaseModel):
    classroom_id: int = Field(..., gt=0)
    target_date: date


class ApplyTemplatePayload(BaseModel):
    template_id: int = Field(..., gt=0)
    entry_ids: list[int] = Field(..., min_length=1, max_length=100)
    only_fill_blank: bool = True


class BatchPublishPayload(BaseModel):
    entry_ids: list[int] = Field(..., min_length=1, max_length=100)


# ── Helpers ───────────────────────────────────────────────────────────────


def _entry_to_dict(entry: StudentContactBookEntry, photos: list[Attachment]) -> dict:
    return {
        "id": entry.id,
        "student_id": entry.student_id,
        "classroom_id": entry.classroom_id,
        "log_date": entry.log_date.isoformat() if entry.log_date else None,
        "mood": entry.mood,
        "meal_lunch": entry.meal_lunch,
        "meal_snack": entry.meal_snack,
        "nap_minutes": entry.nap_minutes,
        "bowel": entry.bowel,
        "temperature_c": (
            float(entry.temperature_c) if entry.temperature_c is not None else None
        ),
        "teacher_note": entry.teacher_note,
        "learning_highlight": entry.learning_highlight,
        "published_at": (
            entry.published_at.isoformat() if entry.published_at else None
        ),
        "version": entry.version,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        "photos": [
            {
                "id": p.id,
                "display_url": f"/api/parent/uploads/portfolio/{p.display_key or p.storage_key}",
                "thumb_url": (
                    f"/api/parent/uploads/portfolio/{p.thumb_key}"
                    if p.thumb_key
                    else None
                ),
                "original_filename": p.original_filename,
            }
            for p in photos
            if p.deleted_at is None
        ],
    }


def _load_photos(session, entry_id: int) -> list[Attachment]:
    return (
        session.query(Attachment)
        .filter(
            Attachment.owner_type == ATTACHMENT_OWNER_CONTACT_BOOK,
            Attachment.owner_id == entry_id,
            Attachment.deleted_at.is_(None),
        )
        .order_by(Attachment.created_at.asc())
        .all()
    )


def _assert_classroom_owned(session, emp_id: int, classroom_id: int) -> None:
    """老師只能操作自己班級的聯絡簿。admin/supervisor 由 require_permission 保護放行。"""
    classroom_ids = _get_teacher_classroom_ids(session, emp_id)
    if classroom_id not in classroom_ids:
        raise HTTPException(status_code=403, detail="此班級不屬於您")


def _parse_if_match(if_match: Optional[str]) -> Optional[int]:
    """解析 If-Match header，支援 W/"3" / "3" / 3 等格式。"""
    if not if_match:
        return None
    val = if_match.strip()
    if val.startswith("W/"):
        val = val[2:].strip()
    val = val.strip('"')
    try:
        return int(val)
    except ValueError:
        return None


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("")
def list_classroom_day(
    classroom_id: int = Query(..., gt=0),
    log_date: date = Query(...),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
):
    """列出某班某日所有聯絡簿（教師可看草稿與已發布）。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        # admin/supervisor 路徑：permissions=-1 或具 admin 角色時允許跨班
        if current_user.get("role") == "teacher":
            _assert_classroom_owned(session, emp.id, classroom_id)
        else:
            classroom = session.query(Classroom).get(classroom_id)
            if not classroom:
                raise HTTPException(status_code=404, detail="班級不存在")

        roster = (
            session.query(Student)
            .filter(Student.classroom_id == classroom_id, Student.is_active.is_(True))
            .order_by(Student.name.asc())
            .all()
        )
        entries = (
            session.query(StudentContactBookEntry)
            .filter(
                StudentContactBookEntry.classroom_id == classroom_id,
                StudentContactBookEntry.log_date == log_date,
                StudentContactBookEntry.deleted_at.is_(None),
            )
            .all()
        )
        entry_by_student = {e.student_id: e for e in entries}

        # Phase 3 N+1 修補：列表前一次 IN clause 取所有 entries 的 photos，
        # 取代 _load_photos 在迴圈內逐 entry 一次 query。
        entry_ids = [e.id for e in entries]
        photos_by_entry: dict[int, list] = {eid: [] for eid in entry_ids}
        if entry_ids:
            attachments = (
                session.query(Attachment)
                .filter(
                    Attachment.owner_type == ATTACHMENT_OWNER_CONTACT_BOOK,
                    Attachment.owner_id.in_(entry_ids),
                    Attachment.deleted_at.is_(None),
                )
                .order_by(Attachment.created_at.asc())
                .all()
            )
            for a in attachments:
                photos_by_entry.setdefault(a.owner_id, []).append(a)

        items = []
        for s in roster:
            entry = entry_by_student.get(s.id)
            photos = photos_by_entry.get(entry.id, []) if entry else []
            items.append(
                {
                    "student_id": s.id,
                    "student_name": s.name,
                    "entry": _entry_to_dict(entry, photos) if entry else None,
                }
            )
        completion = compute_class_completion(
            session, classroom_id=classroom_id, log_date=log_date
        )
        return {
            "classroom_id": classroom_id,
            "log_date": log_date.isoformat(),
            "completion": completion,
            "items": items,
        }
    finally:
        session.close()


@router.post("/batch")
def batch_upsert(
    payload: ContactBookBatchPayload,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
):
    """班級表式批次 upsert（皆預設為草稿）。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        if current_user.get("role") == "teacher":
            _assert_classroom_owned(session, emp.id, payload.classroom_id)

        # 校驗 student 屬於該 classroom
        student_ids = [it.student_id for it in payload.items]
        roster_ids = {
            s.id
            for s in session.query(Student.id)
            .filter(
                Student.classroom_id == payload.classroom_id,
                Student.id.in_(student_ids),
            )
            .all()
        }
        invalid = set(student_ids) - roster_ids
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"以下學生不屬於此班級：{sorted(invalid)}",
            )

        existing = {
            e.student_id: e
            for e in session.query(StudentContactBookEntry)
            .filter(
                StudentContactBookEntry.classroom_id == payload.classroom_id,
                StudentContactBookEntry.log_date == payload.log_date,
                StudentContactBookEntry.deleted_at.is_(None),
                StudentContactBookEntry.student_id.in_(student_ids),
            )
            .all()
        }

        result_ids: list[int] = []
        for it in payload.items:
            entry = existing.get(it.student_id)
            if entry is None:
                entry = StudentContactBookEntry(
                    student_id=it.student_id,
                    classroom_id=payload.classroom_id,
                    log_date=payload.log_date,
                    created_by_employee_id=emp.id,
                )
                session.add(entry)
            entry.mood = it.mood
            entry.meal_lunch = it.meal_lunch
            entry.meal_snack = it.meal_snack
            entry.nap_minutes = it.nap_minutes
            entry.bowel = it.bowel
            entry.temperature_c = it.temperature_c
            entry.teacher_note = it.teacher_note
            entry.learning_highlight = it.learning_highlight
            session.flush()
            result_ids.append(entry.id)

        session.commit()

        request.state.audit_entity_id = str(payload.classroom_id)
        request.state.audit_summary = (
            f"教師批次填聯絡簿：classroom={payload.classroom_id} "
            f"log_date={payload.log_date} 筆數={len(result_ids)}"
        )
        return {
            "classroom_id": payload.classroom_id,
            "log_date": payload.log_date.isoformat(),
            "entry_ids": result_ids,
        }
    finally:
        session.close()


@router.put("/{entry_id}")
def update_entry(
    entry_id: int,
    payload: ContactBookEntryFields,
    request: Request,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
):
    """單筆編輯。若帶 If-Match 需與目前 version 相符。"""
    expected_version = _parse_if_match(if_match)
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        entry = (
            session.query(StudentContactBookEntry)
            .filter(
                StudentContactBookEntry.id == entry_id,
                StudentContactBookEntry.deleted_at.is_(None),
            )
            .first()
        )
        if not entry:
            raise HTTPException(status_code=404, detail="聯絡簿不存在")
        if current_user.get("role") == "teacher":
            _assert_classroom_owned(session, emp.id, entry.classroom_id)

        if expected_version is not None and entry.version != expected_version:
            # 衝突：回 409 + 完整 current_entry payload，前端可局部寫回不必整撈
            current_photos = _load_photos(session, entry.id)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "VERSION_CONFLICT",
                    "message": "聯絡簿已被他人更新，請重新整理後再編輯",
                    "current_version": entry.version,
                    "current_entry": _entry_to_dict(entry, current_photos),
                },
            )

        for field in (
            "mood",
            "meal_lunch",
            "meal_snack",
            "nap_minutes",
            "bowel",
            "temperature_c",
            "teacher_note",
            "learning_highlight",
        ):
            setattr(entry, field, getattr(payload, field))
        entry.version = (entry.version or 1) + 1
        session.commit()
        photos = _load_photos(session, entry.id)

        request.state.audit_entity_id = str(entry.id)
        request.state.audit_summary = (
            f"教師編輯聯絡簿：entry={entry.id} student={entry.student_id} "
            f"version={entry.version}"
        )
        return _entry_to_dict(entry, photos)
    finally:
        session.close()


@router.post("/{entry_id}/publish")
def publish_endpoint(
    entry_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
):
    """發布單筆聯絡簿，觸發 WS 廣播 + LINE 推播。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        entry = (
            session.query(StudentContactBookEntry)
            .filter(StudentContactBookEntry.id == entry_id)
            .first()
        )
        if not entry or entry.deleted_at:
            raise HTTPException(status_code=404, detail="聯絡簿不存在")
        if current_user.get("role") == "teacher":
            _assert_classroom_owned(session, emp.id, entry.classroom_id)

        was_already_published = entry.published_at is not None
        entry = publish_entry(session, entry_id=entry.id, line_service=_line_service)
        session.commit()
        photos = _load_photos(session, entry.id)

        request.state.audit_entity_id = str(entry.id)
        request.state.audit_summary = (
            f"教師發布聯絡簿：entry={entry.id} student={entry.student_id} "
            f"first_publish={not was_already_published}"
        )
        return _entry_to_dict(entry, photos)
    finally:
        session.close()


@router.post("/{entry_id}/photos", status_code=201)
async def upload_photo(
    entry_id: int,
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
):
    """為聯絡簿上傳照片（一次一張）。"""
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _ALLOWED_PHOTO_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的檔案格式：{ext or '未知'}；接受 JPG/PNG/HEIC",
        )
    if is_heic_extension(ext) and not heic_supported():
        raise HTTPException(
            status_code=400, detail="伺服器未安裝 HEIC 解碼套件，請改傳 JPG/PNG"
        )
    content = await read_upload_with_size_check(file, extension=ext)
    validate_file_signature(content, ext)

    from utils.portfolio_storage import get_portfolio_storage

    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        entry = (
            session.query(StudentContactBookEntry)
            .filter(StudentContactBookEntry.id == entry_id)
            .first()
        )
        if not entry or entry.deleted_at:
            raise HTTPException(status_code=404, detail="聯絡簿不存在")
        if current_user.get("role") == "teacher":
            _assert_classroom_owned(session, emp.id, entry.classroom_id)

        storage = get_portfolio_storage()
        stored = storage.put_attachment(content, ext)
        att = Attachment(
            owner_type=ATTACHMENT_OWNER_CONTACT_BOOK,
            owner_id=entry.id,
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
        session.commit()

        request.state.audit_entity_id = str(entry.id)
        request.state.audit_summary = (
            f"教師上傳聯絡簿照片：entry={entry.id} attachment={att.id} "
            f"filename={filename} size={len(content)}B"
        )
        return {
            "id": att.id,
            "display_url": f"/api/parent/uploads/portfolio/{att.display_key or att.storage_key}",
            "thumb_url": (
                f"/api/parent/uploads/portfolio/{att.thumb_key}"
                if att.thumb_key
                else None
            ),
            "original_filename": att.original_filename,
        }
    finally:
        session.close()


@router.get("/unpublished")
def list_unpublished(
    classroom_id: int = Query(..., gt=0),
    log_date: date = Query(...),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
):
    """列某班某日未發布草稿（含學生姓名），便於批次發布。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        if current_user.get("role") == "teacher":
            _assert_classroom_owned(session, emp.id, classroom_id)

        entries = (
            session.query(StudentContactBookEntry)
            .filter(
                StudentContactBookEntry.classroom_id == classroom_id,
                StudentContactBookEntry.log_date == log_date,
                StudentContactBookEntry.deleted_at.is_(None),
                StudentContactBookEntry.published_at.is_(None),
            )
            .all()
        )
        if not entries:
            return {
                "classroom_id": classroom_id,
                "log_date": log_date.isoformat(),
                "items": [],
            }
        student_ids = [e.student_id for e in entries]
        students = {
            s.id: s
            for s in session.query(Student).filter(Student.id.in_(student_ids)).all()
        }
        items = [
            {
                "id": e.id,
                "student_id": e.student_id,
                "student_name": (
                    students.get(e.student_id).name
                    if students.get(e.student_id)
                    else None
                ),
                "version": e.version,
                "updated_at": e.updated_at.isoformat() if e.updated_at else None,
            }
            for e in entries
        ]
        return {
            "classroom_id": classroom_id,
            "log_date": log_date.isoformat(),
            "items": items,
        }
    finally:
        session.close()


@router.post("/copy-from-yesterday")
def copy_from_yesterday(
    payload: CopyYesterdayPayload,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
):
    """把昨日該班所有 entry 欄位複製為今日草稿。已存在當日 entry 的學生 skip。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        if current_user.get("role") == "teacher":
            _assert_classroom_owned(session, emp.id, payload.classroom_id)

        created = copy_yesterday_to_today(
            session,
            classroom_id=payload.classroom_id,
            target_date=payload.target_date,
            created_by_employee_id=emp.id,
        )
        session.commit()

        request.state.audit_entity_id = str(payload.classroom_id)
        request.state.audit_summary = (
            f"教師複製昨日聯絡簿：classroom={payload.classroom_id} "
            f"target_date={payload.target_date} created={created}"
        )
        return {
            "classroom_id": payload.classroom_id,
            "target_date": payload.target_date.isoformat(),
            "created": created,
        }
    finally:
        session.close()


@router.post("/apply-template")
def apply_template(
    payload: ApplyTemplatePayload,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
):
    """把範本欄位套用到指定 entry 列表。預設只填空欄位（不蓋已填值）。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        # 取範本（personal 範本只允許 owner 用；shared 全員可用）
        tpl = (
            session.query(ContactBookTemplate)
            .filter(
                ContactBookTemplate.id == payload.template_id,
                ContactBookTemplate.is_archived.is_(False),
            )
            .first()
        )
        if not tpl:
            raise HTTPException(status_code=404, detail="範本不存在")
        if tpl.scope == "personal" and tpl.owner_user_id != current_user.get("user_id"):
            raise HTTPException(status_code=403, detail="無權使用此個人範本")

        entries = (
            session.query(StudentContactBookEntry)
            .filter(
                StudentContactBookEntry.id.in_(payload.entry_ids),
                StudentContactBookEntry.deleted_at.is_(None),
            )
            .all()
        )
        if not entries:
            raise HTTPException(status_code=404, detail="找不到對應 entry")

        # 班級守衛：所有 entry 必須屬於教師管轄
        if current_user.get("role") == "teacher":
            classroom_ids = {e.classroom_id for e in entries}
            for cid in classroom_ids:
                _assert_classroom_owned(session, emp.id, cid)

        # 已發布 entry 不允許套範本（避免擾動家長已看到的內容）
        already_published = [e.id for e in entries if e.published_at is not None]
        if already_published:
            raise HTTPException(
                status_code=400,
                detail=f"已發布的聯絡簿不可套用範本：{already_published}",
            )

        applied: list[dict] = []
        for e in entries:
            changed = apply_template_fields(
                e,
                tpl.fields or {},
                only_fill_blank=payload.only_fill_blank,
            )
            if changed:
                e.version = (e.version or 1) + 1
                applied.append(
                    {"entry_id": e.id, "changed_fields": changed, "version": e.version}
                )
            else:
                applied.append(
                    {"entry_id": e.id, "changed_fields": [], "version": e.version}
                )
        session.commit()

        request.state.audit_entity_id = str(tpl.id)
        request.state.audit_summary = (
            f"教師套用範本：template={tpl.id} entries={len(entries)} "
            f"only_fill_blank={payload.only_fill_blank}"
        )
        return {"template_id": tpl.id, "results": applied}
    finally:
        session.close()


@router.post("/batch-publish")
def batch_publish(
    payload: BatchPublishPayload,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
):
    """一鍵批次發布草稿。逐筆呼叫 publish_entry，回傳每筆成功 / 失敗。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        entries = (
            session.query(StudentContactBookEntry)
            .filter(
                StudentContactBookEntry.id.in_(payload.entry_ids),
                StudentContactBookEntry.deleted_at.is_(None),
            )
            .all()
        )
        if not entries:
            raise HTTPException(status_code=404, detail="找不到對應 entry")

        # 班級守衛
        if current_user.get("role") == "teacher":
            classroom_ids = {e.classroom_id for e in entries}
            for cid in classroom_ids:
                _assert_classroom_owned(session, emp.id, cid)

        results: list[dict] = []
        success_ids: list[int] = []
        for entry in entries:
            try:
                publish_entry(session, entry_id=entry.id, line_service=_line_service)
                success_ids.append(entry.id)
                results.append({"entry_id": entry.id, "status": "ok"})
            except Exception as exc:
                logger.warning("batch_publish 單筆失敗 entry=%d: %s", entry.id, exc)
                results.append(
                    {"entry_id": entry.id, "status": "error", "message": str(exc)}
                )
        session.commit()

        request.state.audit_entity_id = ",".join(map(str, success_ids))
        request.state.audit_summary = (
            f"教師批次發布聯絡簿：success={len(success_ids)}/{len(entries)}"
        )
        return {"results": results, "success_count": len(success_ids)}
    finally:
        session.close()


@router.delete("/{entry_id}/photos/{attachment_id}")
def delete_photo(
    entry_id: int,
    attachment_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
):
    """軟刪聯絡簿照片。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        entry = (
            session.query(StudentContactBookEntry)
            .filter(StudentContactBookEntry.id == entry_id)
            .first()
        )
        if not entry or entry.deleted_at:
            raise HTTPException(status_code=404, detail="聯絡簿不存在")
        if current_user.get("role") == "teacher":
            _assert_classroom_owned(session, emp.id, entry.classroom_id)

        att = (
            session.query(Attachment)
            .filter(
                Attachment.id == attachment_id,
                Attachment.owner_type == ATTACHMENT_OWNER_CONTACT_BOOK,
                Attachment.owner_id == entry_id,
            )
            .first()
        )
        if not att:
            raise HTTPException(status_code=404, detail="附件不存在")
        if att.deleted_at:
            return {"message": "附件已刪除"}
        att.deleted_at = datetime.now()
        session.commit()

        request.state.audit_entity_id = str(entry_id)
        request.state.audit_summary = (
            f"教師刪除聯絡簿照片：entry={entry_id} attachment={attachment_id}"
        )
        return {"message": "刪除成功"}
    finally:
        session.close()
