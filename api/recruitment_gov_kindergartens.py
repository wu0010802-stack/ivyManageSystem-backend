"""
api/recruitment_gov_kindergartens.py — 教育部幼兒園公開資料 API endpoints

提供高雄市幼兒園的完整政府資料查詢（來自教育部 ECE 系統爬蟲快取），
以及手動觸發爬蟲同步的端點。
"""

import logging
import threading
from typing import Optional

from fastapi import APIRouter, Depends, Query

from models.base import session_scope
from models.recruitment import CompetitorSchool
from services import moe_kindergarten_scraper as moe_scraper
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/recruitment/gov-kindergartens", tags=["recruitment-gov-kindergartens"]
)


def _to_dict(school: CompetitorSchool) -> dict:
    return {
        "id": school.id,
        "source_school_id": school.source_school_id,
        "school_name": school.school_name,
        "school_type": school.school_type,
        "pre_public_type": school.pre_public_type,
        "owner_name": school.owner_name,
        "phone": school.phone,
        "address": school.address,
        "district": school.district,
        "city": school.city,
        "approved_capacity": school.approved_capacity,
        "approved_date": school.approved_date,
        "total_area_sqm": school.total_area_sqm,
        "monthly_fee": school.monthly_fee,
        "has_penalty": school.has_penalty,
        "website": school.website,
        "is_active": school.is_active,
        "source_updated_at": (
            school.source_updated_at.isoformat() if school.source_updated_at else None
        ),
    }


@router.get("")
def list_gov_kindergartens(
    city: Optional[str] = Query(None, description="縣市篩選，如「高雄市」"),
    district: Optional[str] = Query(None, description="行政區篩選，如「三民區」"),
    school_type: Optional[str] = Query(None, description="設立別篩選，如「私立」"),
    pre_public: Optional[str] = Query(None, description="準公共篩選，如「有」"),
    search: Optional[str] = Query(None, description="園所名稱關鍵字搜尋"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """列出所有快取的幼兒園資料，支援多條件篩選。"""
    with session_scope() as sess:
        q = sess.query(CompetitorSchool)

        if city:
            q = q.filter(CompetitorSchool.city == city)
        if district:
            q = q.filter(CompetitorSchool.district == district)
        if school_type:
            q = q.filter(CompetitorSchool.school_type == school_type)
        if pre_public:
            q = q.filter(CompetitorSchool.pre_public_type == pre_public)
        if search:
            q = q.filter(CompetitorSchool.school_name.ilike(f"%{search}%"))

        total = q.count()
        records = (
            q.order_by(CompetitorSchool.district, CompetitorSchool.school_name)
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        # 查最後同步時間
        sync_status = moe_scraper.get_sync_status()
        synced_at = sync_status.get("last_synced_at")

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "synced_at": synced_at,
            "schools": [_to_dict(r) for r in records],
        }


@router.get("/sync-status")
def get_gov_kindergartens_sync_status(
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """查詢教育部資料同步進度與最後同步時間。"""
    return moe_scraper.get_sync_status()


@router.post("/sync")
def trigger_gov_kindergartens_sync(
    background: bool = Query(
        True, description="是否背景執行（預設 True；False 則同步等待完成）"
    ),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    """
    觸發教育部高雄市幼兒園資料爬蟲。

    預設在背景執行（background=true），立即回傳 accepted 狀態。
    設 background=false 則同步等待，適用於腳本或測試情境。
    """
    status = moe_scraper.get_sync_status()
    if status.get("sync_in_progress"):
        return {
            "status": "already_running",
            "message": "已有同步作業正在執行，請稍後查詢進度",
        }

    if background:
        t = threading.Thread(target=moe_scraper.sync_moe_kindergartens, daemon=True)
        t.start()
        logger.info("[MOE 爬蟲] 背景同步已啟動")
        return {
            "status": "accepted",
            "message": "背景同步已啟動，請透過 /sync-status 查詢進度",
        }

    result = moe_scraper.sync_moe_kindergartens()
    return result
