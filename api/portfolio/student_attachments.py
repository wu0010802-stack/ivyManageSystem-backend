"""Student-centric attachment aggregator (admin).

Endpoint:
- GET /api/students/{student_id}/attachments?owner_type=&since=&until=&skip=&limit=

回傳該學生跨 owner_type (observation / contact_book_entry / medication_order / report)
的所有 image/* attachments。

實作走單一 SQL：
- mime_type LIKE 'image/%' 在 SQL 端過濾（避免 Python-side filter）
- 4 個 owner_type 用 OR + IN(subquery) 一次查完，total/offset/limit 全在 SQL 端
- until 用 < (until + 1 day) 才能涵蓋當天 23:59:59 的 timestamp
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, or_

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
from utils.audit import write_explicit_audit
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


def _owner_id_subquery(session, owner_type: str, student_id: int):
    """回傳該 owner_type 下，屬於這位學生的 owner_id subquery（admin 視角）.

    admin 端不過濾 published / deleted；家長端有獨立 _parent_owner_ids 在
    api/parent_portal/photos.py，請勿在此模組共用。
    """
    if owner_type == ATTACHMENT_OWNER_OBSERVATION:
        return (
            session.query(StudentObservation.id)
            .filter(
                StudentObservation.student_id == student_id,
                StudentObservation.deleted_at.is_(None),
            )
            .scalar_subquery()
        )
    if owner_type == ATTACHMENT_OWNER_CONTACT_BOOK:
        return (
            session.query(StudentContactBookEntry.id)
            .filter(StudentContactBookEntry.student_id == student_id)
            .scalar_subquery()
        )
    if owner_type == ATTACHMENT_OWNER_MEDICATION_ORDER:
        return (
            session.query(StudentMedicationOrder.id)
            .filter(StudentMedicationOrder.student_id == student_id)
            .scalar_subquery()
        )
    if owner_type == ATTACHMENT_OWNER_REPORT:
        return (
            session.query(StudentGrowthReport.id)
            .filter(StudentGrowthReport.student_id == student_id)
            .scalar_subquery()
        )
    return None


@router.get("/{student_id}/attachments")
async def list_student_attachments(
    student_id: int,
    request: Request,
    owner_type: Optional[str] = Query(None, description="篩選單一 owner_type"),
    since: Optional[date] = Query(None),
    until: Optional[date] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id, code=Permission.PORTFOLIO_READ.value)
            student = session.query(Student).filter_by(id=student_id).first()
            if not student:
                raise HTTPException(status_code=404, detail="學生不存在")

            if owner_type and owner_type not in SUPPORTED_OWNER_TYPES:
                raise HTTPException(status_code=422, detail="不支援的 owner_type")

            # F-V6-03：跨模組 PII 聚合端點補敏感讀取 audit（對齊 7a25d767）
            write_explicit_audit(
                request,
                action="READ",
                entity_type="student",
                entity_id=str(student_id),
                summary=f"portfolio 跨模組附件聚合：student_id={student_id}",
                changes={
                    "owner_type_filter": owner_type or "all",
                    "since": since.isoformat() if since else None,
                    "until": until.isoformat() if until else None,
                },
            )

            target_owner_types = (
                [owner_type] if owner_type else list(SUPPORTED_OWNER_TYPES)
            )

            owner_clauses = []
            for ot in target_owner_types:
                sq = _owner_id_subquery(session, ot, student_id)
                if sq is None:
                    continue
                owner_clauses.append(
                    and_(
                        Attachment.owner_type == ot,
                        Attachment.owner_id.in_(sq),
                    )
                )
            if not owner_clauses:
                return {"total": 0, "items": []}

            q = session.query(Attachment).filter(
                Attachment.deleted_at.is_(None),
                Attachment.mime_type.like("image/%"),
                or_(*owner_clauses),
            )
            if since:
                q = q.filter(Attachment.created_at >= since)
            if until:
                # date 比較會被 cast 成 00:00:00；用 < (until + 1 day) 才能涵蓋
                # 當天上傳的 timestamp（agent P2 #8）
                q = q.filter(Attachment.created_at < until + timedelta(days=1))

            total = q.count()
            rows = (
                q.order_by(Attachment.created_at.desc()).offset(skip).limit(limit).all()
            )
            items = [_attachment_to_dict(a) for a in rows]
            return {"total": total, "items": items}
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="查詢學生附件失敗")
