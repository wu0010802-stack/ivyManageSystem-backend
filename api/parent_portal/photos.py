"""api/parent_portal/photos.py — 家長端照片牆 (read-only image attachments).

Endpoint:
- GET /api/parent/photos?student_id=&skip=&limit=

家長視角的 owner_id 反查與 admin 不同：
- 聯絡簿草稿（published_at IS NULL）與軟刪聯絡簿照片不可露出
- 軟刪觀察照片不可露出
- 未 ready 的成長報告附件不可露出（與 /growth-reports 列表口徑一致）

故本檔不再共用 admin `_student_owner_ids`，改用本地 `_parent_owner_ids` 強制
家長端可見性過濾。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.attachments import _attachment_to_dict
from api.portfolio.student_attachments import SUPPORTED_OWNER_TYPES, _is_image
from models.contact_book import StudentContactBookEntry
from models.database import (
    Attachment,
    StudentGrowthReport,
    StudentMedicationOrder,
    StudentObservation,
    get_session,
)
from models.portfolio import (
    ATTACHMENT_OWNER_CONTACT_BOOK,
    ATTACHMENT_OWNER_MEDICATION_ORDER,
    ATTACHMENT_OWNER_OBSERVATION,
    ATTACHMENT_OWNER_REPORT,
    REPORT_STATUS_READY,
)
from utils.auth import require_parent_role
from utils.errors import raise_safe_500

from ._shared import _assert_student_owned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/photos", tags=["parent-photos"])


def _parent_owner_ids(session, owner_type: str, student_id: int) -> list[int]:
    """家長視角下，可見的 owner_id list（已套用發布/軟刪過濾）."""
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
            .filter(
                StudentContactBookEntry.student_id == student_id,
                StudentContactBookEntry.deleted_at.is_(None),
                StudentContactBookEntry.published_at.isnot(None),
            )
            .all()
        )
    elif owner_type == ATTACHMENT_OWNER_MEDICATION_ORDER:
        # MedicationOrder 無 deleted_at / published_at；oder_date 與 source 由
        # 家長端管理頁另控，照片可隨 order 一併露出。
        rows = (
            session.query(StudentMedicationOrder.id)
            .filter(StudentMedicationOrder.student_id == student_id)
            .all()
        )
    elif owner_type == ATTACHMENT_OWNER_REPORT:
        # 與 /growth-reports 列表一致：未 ready 的報告附件不露出
        rows = (
            session.query(StudentGrowthReport.id)
            .filter(
                StudentGrowthReport.student_id == student_id,
                StudentGrowthReport.status == REPORT_STATUS_READY,
            )
            .all()
        )
    else:
        return []
    return [r[0] for r in rows]


@router.get("")
async def parent_list_photos(
    student_id: int = Query(...),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_parent_role()),
) -> dict:
    try:
        session = get_session()
        try:
            user_id = current_user["user_id"]
            _assert_student_owned(session, user_id, student_id)

            all_items: list[dict] = []
            for ot in SUPPORTED_OWNER_TYPES:
                owner_ids = _parent_owner_ids(session, ot, student_id)
                if not owner_ids:
                    continue
                rows = (
                    session.query(Attachment)
                    .filter(
                        Attachment.owner_type == ot,
                        Attachment.owner_id.in_(owner_ids),
                        Attachment.deleted_at.is_(None),
                    )
                    .order_by(Attachment.created_at.desc())
                    .all()
                )
                for a in rows:
                    if not _is_image(a.mime_type):
                        continue
                    all_items.append(_attachment_to_dict(a))

            all_items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return {
                "total": len(all_items),
                "items": all_items[skip : skip + limit],
            }
        finally:
            session.close()
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="家長端照片牆查詢失敗")
