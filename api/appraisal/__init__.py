"""api/appraisal 子套件：彙整半年考核系統各 router。

依序註冊：cycles / participants / score_items / summaries / bonus_rates /
score_item_catalog / reports。
"""

from fastapi import APIRouter

from .bonus_rates import router as bonus_rates_router
from .cycles import router as cycles_router
from .participants import router as participants_router
from .reports import router as reports_router
from .score_item_catalog import router as score_item_catalog_router
from .score_items import router as score_items_router
from .summaries import router as summaries_router

appraisal_router = APIRouter(prefix="/api/appraisal", tags=["appraisal"])
appraisal_router.include_router(cycles_router)
appraisal_router.include_router(participants_router)
appraisal_router.include_router(score_items_router)
appraisal_router.include_router(summaries_router)
appraisal_router.include_router(bonus_rates_router)
appraisal_router.include_router(score_item_catalog_router)
appraisal_router.include_router(reports_router)

__all__ = ["appraisal_router"]
