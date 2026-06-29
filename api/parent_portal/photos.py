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
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from api.portfolio.student_attachments import SUPPORTED_OWNER_TYPES
from models.contact_book import StudentContactBookEntry
from models.database import (
    Attachment,
    StudentGrowthReport,
    StudentMedicationOrder,
    StudentObservation,
)
from models.portfolio import (
    ATTACHMENT_OWNER_CONTACT_BOOK,
    ATTACHMENT_OWNER_MEDICATION_ORDER,
    ATTACHMENT_OWNER_OBSERVATION,
    ATTACHMENT_OWNER_REPORT,
    REPORT_STATUS_READY,
)
from utils.exceptions import BusinessError
from utils.auth import require_parent_role
from utils.errors import raise_safe_500
from utils.portfolio_storage import PORTFOLIO_MODULE

from ._dependencies import get_parent_db
from ._shared import _assert_student_owned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/photos", tags=["parent-photos"])


def _parent_url_for_key(key: str) -> str:
    """生成家長端附件 URL。

    與 api/attachments.py:_url_for_key 對偶；但 staff 端走
    `/api/uploads/portfolio/{key}`（需 PORTFOLIO_READ），家長 permissions=0
    沒此 bit，故必須改走 `/api/parent/uploads/portfolio/{key}` 即
    api/parent_portal/parent_downloads.py 提供的家長專用下載路由。

    bug sweep round 4 (2026-05-14) B8：原本 photos.py 直接 reuse
    `_attachment_to_dict` 生成 admin URL，所有 <img> 對家長一律 403 變破圖。
    """
    return f"/api/parent/uploads/{PORTFOLIO_MODULE}/{key}"


def _parent_attachment_to_dict(att: Attachment) -> dict:
    return {
        "id": att.id,
        "owner_type": att.owner_type,
        "owner_id": att.owner_id,
        "original_filename": att.original_filename,
        "mime_type": att.mime_type,
        "size_bytes": att.size_bytes,
        "url": _parent_url_for_key(att.storage_key),
        "display_url": (
            _parent_url_for_key(att.display_key) if att.display_key else None
        ),
        "thumb_url": (_parent_url_for_key(att.thumb_key) if att.thumb_key else None),
        "uploaded_by": att.uploaded_by,
        "created_at": att.created_at.isoformat() if att.created_at else None,
    }


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
def parent_list_photos(
    student_id: int = Query(...),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
) -> dict:
    try:
        user_id = current_user["user_id"]
        _assert_student_owned(session, user_id, student_id)

        # B1（2026-06-29 效能健檢）：各 owner type 算可見 owner_id 後，組成
        # OR(owner_type==ot AND owner_id IN ids)，單一查詢 + SQL 圖片過濾
        # （_is_image == mime LIKE 'image/%'）+ ORDER BY created_at DESC +
        # SQL LIMIT/OFFSET，取代 4 個 owner type 各無界 .all() 載整段歷史相片
        # 再 Python 切片（attachments 是每日成長最快的表）。
        owner_conds = []
        for ot in SUPPORTED_OWNER_TYPES:
            owner_ids = _parent_owner_ids(session, ot, student_id)
            if owner_ids:
                owner_conds.append(
                    and_(
                        Attachment.owner_type == ot,
                        Attachment.owner_id.in_(owner_ids),
                    )
                )
        if not owner_conds:
            return {"total": 0, "items": []}

        base = session.query(Attachment).filter(
            or_(*owner_conds),
            Attachment.deleted_at.is_(None),
            Attachment.mime_type.like("image/%"),
        )
        total = base.count()
        rows = (
            base.order_by(Attachment.created_at.desc(), Attachment.id.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
        return {
            "total": total,
            "items": [_parent_attachment_to_dict(a) for a in rows],
        }
    except (HTTPException, BusinessError):
        raise
    except Exception as e:
        raise_safe_500(e, context="家長端照片牆查詢失敗")
