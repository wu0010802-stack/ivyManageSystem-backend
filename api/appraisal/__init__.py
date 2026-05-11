"""api/appraisal 子套件：彙整考核系統各 router。

模式參考 api/activity/__init__.py：
  - 每個子 router 為獨立 .py 檔
  - 在此彙整後對外暴露 appraisal_router
  - 後續 task 依序 include：participants / events / summaries / bonus_rates / penalty_catalog / reports
"""

from fastapi import APIRouter

from .bonus_rates import router as bonus_rates_router
from .cycles import router as cycles_router
from .events import router as events_router
from .participants import router as participants_router
from .penalty_catalog import router as penalty_catalog_router
from .reports import router as reports_router
from .summaries import router as summaries_router

appraisal_router = APIRouter(prefix="/api/appraisal", tags=["appraisal"])
appraisal_router.include_router(cycles_router)
appraisal_router.include_router(participants_router)
appraisal_router.include_router(events_router)
appraisal_router.include_router(summaries_router)
appraisal_router.include_router(bonus_rates_router)
appraisal_router.include_router(penalty_catalog_router)
appraisal_router.include_router(reports_router)

__all__ = ["appraisal_router"]
