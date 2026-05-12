"""api/parent_portal/photos.py — 家長端照片牆 (read-only image attachments).

Endpoint:
- GET /api/parent/photos?student_id=&skip=&limit=

複用 admin student_attachments 的 owner-ids 反查 + image filter，但走 parent IDOR.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.attachments import _attachment_to_dict
from api.portfolio.student_attachments import (
    SUPPORTED_OWNER_TYPES,
    _is_image,
    _student_owner_ids,
)
from models.database import Attachment, get_session
from utils.auth import require_parent_role
from utils.errors import raise_safe_500

from ._shared import _assert_student_owned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/photos", tags=["parent-photos"])


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
                owner_ids = _student_owner_ids(session, ot, student_id)
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
