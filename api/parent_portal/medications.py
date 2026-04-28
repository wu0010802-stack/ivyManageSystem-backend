"""api/parent_portal/medications.py — 家長端用藥單

家長可在 LIFF 內直接提交當日用藥單；不走審核，提交即生效，
老師端打卡仍走既有 staff endpoint（POST /api/medication-logs/{log_id}/administer）。

端點：
- GET    /api/parent/medication-orders?student_id&from&to       列表
- GET    /api/parent/medication-orders/{order_id}                detail（含 logs / photos）
- POST   /api/parent/medication-orders                           建單；過敏軟警告
- POST   /api/parent/medication-orders/{order_id}/photos         上傳藥袋/處方照
- DELETE /api/parent/medication-orders/{order_id}/photos/{att}   軟刪附件

過敏軟警告：建單前比對 StudentAllergy（active=true），若藥名與任一過敏原
字面相關，回 409 ALLERGY_WARNING；前端再帶 acknowledge_allergy_warning=true
重送即放行（記入 audit summary）。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime
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
from pydantic import BaseModel, Field, field_validator

from models.database import (
    Attachment,
    StudentMedicationLog,
    StudentMedicationOrder,
    get_session,
)
from models.portfolio import (
    ATTACHMENT_OWNER_MEDICATION_ORDER,
    MEDICATION_SOURCE_PARENT,
)
from services.medication_service import (
    create_order_with_logs,
    find_allergy_conflicts,
)
from utils.auth import require_parent_role
from utils.file_upload import (
    read_upload_with_size_check,
    validate_file_signature,
)
from utils.portfolio_storage import (
    heic_supported,
    is_heic_extension,
)

from ._shared import _assert_student_owned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/medication-orders", tags=["parent-medications"])

_TIME_SLOT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# 家長端附件白名單：照片或處方掃描檔
_PARENT_MED_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".pdf"}


class ParentMedicationOrderCreate(BaseModel):
    student_id: int = Field(..., gt=0)
    order_date: date
    medication_name: str = Field(..., min_length=1, max_length=100)
    dose: str = Field(..., min_length=1, max_length=50)
    time_slots: list[str] = Field(..., min_length=1, max_length=10)
    note: Optional[str] = Field(default=None, max_length=500)
    acknowledge_allergy_warning: bool = Field(
        default=False,
        description="第二次提交時帶 true 以繞過過敏軟警告（前端會在 409 後彈框讓家長確認）",
    )

    @field_validator("time_slots")
    @classmethod
    def _validate_slots(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        for slot in v:
            if not _TIME_SLOT_RE.match(slot):
                raise ValueError(f"時段格式錯誤（應為 HH:MM）：{slot}")
            if slot in seen:
                raise ValueError(f"時段重複：{slot}")
            seen.add(slot)
        return sorted(v)


# ── to_dict helpers（與 staff 端格式對齊，但簡化欄位） ─────────────────────


def _log_to_dict(lg: StudentMedicationLog) -> dict:
    if lg.correction_of is not None:
        status = "correction"
    elif lg.administered_at is not None:
        status = "administered"
    elif lg.skipped:
        status = "skipped"
    else:
        status = "pending"
    return {
        "id": lg.id,
        "order_id": lg.order_id,
        "scheduled_time": lg.scheduled_time,
        "status": status,
        "administered_at": (
            lg.administered_at.isoformat() if lg.administered_at else None
        ),
        "skipped": lg.skipped,
        "skipped_reason": lg.skipped_reason,
        "note": lg.note,
    }


def _attachment_to_dict(att: Attachment) -> dict:
    """parent 端 URL 走 /api/parent/uploads/portfolio/{key}（見 parent_downloads.py）。"""
    return {
        "id": att.id,
        "original_filename": att.original_filename,
        "mime_type": att.mime_type,
        "size_bytes": att.size_bytes,
        "url": f"/api/parent/uploads/portfolio/{att.storage_key}",
        "display_url": (
            f"/api/parent/uploads/portfolio/{att.display_key}"
            if att.display_key
            else None
        ),
        "thumb_url": (
            f"/api/parent/uploads/portfolio/{att.thumb_key}" if att.thumb_key else None
        ),
        "created_at": att.created_at.isoformat() if att.created_at else None,
    }


def _order_to_dict(
    order: StudentMedicationOrder,
    logs: list[StudentMedicationLog],
    photos: list[Attachment],
) -> dict:
    return {
        "id": order.id,
        "student_id": order.student_id,
        "order_date": order.order_date.isoformat(),
        "medication_name": order.medication_name,
        "dose": order.dose,
        "time_slots": list(order.time_slots or []),
        "note": order.note,
        "source": order.source,
        "created_by": order.created_by,
        "logs": [_log_to_dict(lg) for lg in logs],
        "photos": [_attachment_to_dict(p) for p in photos],
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def _load_logs(session, order_id: int) -> list[StudentMedicationLog]:
    return (
        session.query(StudentMedicationLog)
        .filter(StudentMedicationLog.order_id == order_id)
        .order_by(
            StudentMedicationLog.scheduled_time.asc(),
            StudentMedicationLog.id.asc(),
        )
        .all()
    )


def _load_photos(session, order_id: int) -> list[Attachment]:
    return (
        session.query(Attachment)
        .filter(
            Attachment.owner_type == ATTACHMENT_OWNER_MEDICATION_ORDER,
            Attachment.owner_id == order_id,
            Attachment.deleted_at.is_(None),
        )
        .order_by(Attachment.id.asc())
        .all()
    )


def _get_order_for_parent(
    session, *, user_id: int, order_id: int
) -> StudentMedicationOrder:
    """查 order 並做 IDOR：parent 必須是該 student 的監護人。"""
    o = (
        session.query(StudentMedicationOrder)
        .filter(StudentMedicationOrder.id == order_id)
        .first()
    )
    if not o:
        raise HTTPException(status_code=404, detail="用藥單不存在")
    _assert_student_owned(session, user_id, o.student_id)
    return o


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("")
def list_medication_orders(
    student_id: int = Query(..., gt=0),
    date_from: Optional[date] = Query(None, alias="from"),
    date_to: Optional[date] = Query(None, alias="to"),
    current_user: dict = Depends(require_parent_role()),
):
    """列出某學生的用藥單（含老師建立 + 家長自建，不過濾 source）。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        _assert_student_owned(session, user_id, student_id)
        query = session.query(StudentMedicationOrder).filter(
            StudentMedicationOrder.student_id == student_id
        )
        if date_from:
            query = query.filter(StudentMedicationOrder.order_date >= date_from)
        if date_to:
            query = query.filter(StudentMedicationOrder.order_date <= date_to)
        orders = query.order_by(
            StudentMedicationOrder.order_date.desc(),
            StudentMedicationOrder.id.desc(),
        ).all()
        items = [
            _order_to_dict(o, _load_logs(session, o.id), _load_photos(session, o.id))
            for o in orders
        ]
        return {"items": items, "total": len(items)}
    finally:
        session.close()


@router.get("/{order_id}")
def get_medication_order(
    order_id: int,
    current_user: dict = Depends(require_parent_role()),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        o = _get_order_for_parent(session, user_id=user_id, order_id=order_id)
        return _order_to_dict(o, _load_logs(session, o.id), _load_photos(session, o.id))
    finally:
        session.close()


@router.post("", status_code=201)
def create_medication_order(
    payload: ParentMedicationOrderCreate,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
):
    """家長提交用藥單。

    過敏軟警告：第一次提交且藥名與過敏原相關 → 409 ALLERGY_WARNING；
    前端帶 acknowledge_allergy_warning=true 重送即可放行。
    """
    user_id = current_user["user_id"]
    session = get_session()
    try:
        _assert_student_owned(session, user_id, payload.student_id)

        # 過敏軟警告
        conflicts = find_allergy_conflicts(
            session,
            student_id=payload.student_id,
            medication_name=payload.medication_name,
        )
        if conflicts and not payload.acknowledge_allergy_warning:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ALLERGY_WARNING",
                    "message": "用藥名稱可能與孩童過敏原相關，請確認後重送並帶 acknowledge_allergy_warning=true",
                    "allergens": [
                        {
                            "id": a.id,
                            "allergen": a.allergen,
                            "severity": a.severity,
                            "reaction_symptom": a.reaction_symptom,
                        }
                        for a in conflicts
                    ],
                },
            )

        order = create_order_with_logs(
            session,
            student_id=payload.student_id,
            order_date=payload.order_date,
            medication_name=payload.medication_name,
            dose=payload.dose,
            time_slots=payload.time_slots,
            note=payload.note,
            created_by=user_id,
            source=MEDICATION_SOURCE_PARENT,
        )
        session.commit()
        session.refresh(order)
        logs = _load_logs(session, order.id)

        # Audit
        request.state.audit_entity_id = str(order.id)
        warning_note = (
            f" allergy_overridden={[a.allergen for a in conflicts]}"
            if conflicts
            else ""
        )
        request.state.audit_summary = (
            f"家長提交用藥單：student_id={payload.student_id} "
            f"order_id={order.id} medication={payload.medication_name} "
            f"slots={payload.time_slots}{warning_note}"
        )
        logger.info(
            "家長提交用藥單：student_id=%d order_id=%d slots=%s allergy_override=%s parent_user=%d",
            payload.student_id,
            order.id,
            payload.time_slots,
            bool(conflicts),
            user_id,
        )
        return _order_to_dict(order, logs, [])
    finally:
        session.close()


@router.post("/{order_id}/photos", status_code=201)
async def upload_medication_photo(
    order_id: int,
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(require_parent_role()),
):
    """為已建立的用藥單上傳藥袋/處方照（一次一張）。"""
    user_id = current_user["user_id"]

    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _PARENT_MED_ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的檔案格式：{ext or '未知'}；接受 JPG/PNG/HEIC/PDF",
        )
    if is_heic_extension(ext) and not heic_supported():
        raise HTTPException(
            status_code=400,
            detail="伺服器未安裝 HEIC 解碼套件，請改傳 JPG/PNG",
        )
    content = await read_upload_with_size_check(file, extension=ext)
    validate_file_signature(content, ext)

    # 延後 import，避免 attachments router 與本 module 之間的循環
    from utils.portfolio_storage import get_portfolio_storage

    session = get_session()
    try:
        # IDOR
        order = _get_order_for_parent(session, user_id=user_id, order_id=order_id)

        storage = get_portfolio_storage()
        stored = storage.put_attachment(content, ext)

        att = Attachment(
            owner_type=ATTACHMENT_OWNER_MEDICATION_ORDER,
            owner_id=order.id,
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
        session.commit()

        request.state.audit_entity_id = str(order.id)
        request.state.audit_summary = (
            f"家長上傳用藥單附件：order_id={order.id} "
            f"attachment_id={att.id} filename={filename} size={len(content)}B"
        )
        logger.info(
            "家長上傳用藥附件：order_id=%d attachment_id=%d size=%d parent_user=%d",
            order.id,
            att.id,
            len(content),
            user_id,
        )
        return _attachment_to_dict(att)
    finally:
        session.close()


@router.delete("/{order_id}/photos/{attachment_id}")
def delete_medication_photo(
    order_id: int,
    attachment_id: int,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
):
    """軟刪除用藥單附件。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        # IDOR：order 必須屬此 parent
        _get_order_for_parent(session, user_id=user_id, order_id=order_id)
        att = (
            session.query(Attachment)
            .filter(
                Attachment.id == attachment_id,
                Attachment.owner_type == ATTACHMENT_OWNER_MEDICATION_ORDER,
                Attachment.owner_id == order_id,
            )
            .first()
        )
        if not att:
            raise HTTPException(status_code=404, detail="附件不存在")
        if att.deleted_at:
            return {"message": "附件已刪除"}
        att.deleted_at = datetime.now()
        session.commit()

        request.state.audit_entity_id = str(order_id)
        request.state.audit_summary = (
            f"家長刪除用藥單附件：order_id={order_id} attachment_id={attachment_id}"
        )
        return {"message": "刪除成功"}
    finally:
        session.close()
