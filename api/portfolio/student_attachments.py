"""Student-centric attachment aggregator (admin).

Endpoint:
- GET /api/students/{student_id}/attachments?owner_type=&since=&until=&skip=&limit=

回傳該學生跨 owner_type (observation / contact_book_entry / medication_order / report)
的所有 image/* attachments。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.attachments import _attachment_to_dict
from models.database import (
    Attachment,
    Student,
    StudentContactBookEntry,
    StudentGrowthReport,
    StudentMedicationOrder,
    StudentObservation,
    session_scope,
)
from models.portfolio import (
    ATTACHMENT_OWNER_CONTACT_BOOK,
    ATTACHMENT_OWNER_MEDICATION_ORDER,
    ATTACHMENT_OWNER_OBSERVATION,
    ATTACHMENT_OWNER_REPORT,
)
from utils.auth import require_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/students", tags=["portfolio-student-attachments"])


SUPPORTED_OWNER_TYPES = (
    ATTACHMENT_OWNER_OBSERVATION,
    ATTACHMENT_OWNER_CONTACT_BOOK,
    ATTACHMENT_OWNER_MEDICATION_ORDER,
    ATTACHMENT_OWNER_REPORT,
)


def _is_image(mime: Optional[str]) -> bool:
    return bool(mime) and mime.startswith("image/")


def _student_owner_ids(session, owner_type: str, student_id: int) -> list[int]:
    """回傳該 owner_type 下，屬於這位學生的 owner_id list."""
    if owner_type == ATTACHMENT_OWNER_OBSERVATION:
        rows = (
            session.query(StudentObservation.id)
            .filter(
                StudentObservation.student_id == student_id,
                StudentObservation.deleted_at.is_(None),
            )
            .all()
        )
    elif owner_type == ATTACHMENT_OWNER_CONTACT_BOOK:
        rows = (
            session.query(StudentContactBookEntry.id)
            .filter(StudentContactBookEntry.student_id == student_id)
            .all()
        )
    elif owner_type == ATTACHMENT_OWNER_MEDICATION_ORDER:
        rows = (
            session.query(StudentMedicationOrder.id)
            .filter(StudentMedicationOrder.student_id == student_id)
            .all()
        )
    elif owner_type == ATTACHMENT_OWNER_REPORT:
        rows = (
            session.query(StudentGrowthReport.id)
            .filter(StudentGrowthReport.student_id == student_id)
            .all()
        )
    else:
        return []
    return [r[0] for r in rows]


@router.get("/{student_id}/attachments")
async def list_student_attachments(
    student_id: int,
    owner_type: Optional[str] = Query(None, description="篩選單一 owner_type"),
    since: Optional[date] = Query(None),
    until: Optional[date] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            student = session.query(Student).filter_by(id=student_id).first()
            if not student:
                raise HTTPException(status_code=404, detail="學生不存在")

            if owner_type and owner_type not in SUPPORTED_OWNER_TYPES:
                raise HTTPException(status_code=422, detail="不支援的 owner_type")

            target_owner_types = (
                [owner_type] if owner_type else list(SUPPORTED_OWNER_TYPES)
            )

            all_items: list[dict] = []
            for ot in target_owner_types:
                owner_ids = _student_owner_ids(session, ot, student_id)
                if not owner_ids:
                    continue
                q = session.query(Attachment).filter(
                    Attachment.owner_type == ot,
                    Attachment.owner_id.in_(owner_ids),
                    Attachment.deleted_at.is_(None),
                )
                if since:
                    q = q.filter(Attachment.created_at >= since)
                if until:
                    q = q.filter(Attachment.created_at <= until)
                rows = q.order_by(Attachment.created_at.desc()).all()
                for a in rows:
                    if not _is_image(a.mime_type):
                        continue
                    all_items.append(_attachment_to_dict(a))

            all_items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            paged = all_items[skip : skip + limit]
            return {
                "total": len(all_items),
                "items": paged,
            }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="查詢學生附件失敗")
