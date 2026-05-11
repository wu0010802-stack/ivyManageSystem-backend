"""api/appraisal 子套件：彙整考核系統各 router。

模式參考 api/activity/__init__.py：
  - 每個子 router 為獨立 .py 檔
  - 在此彙整後對外暴露 appraisal_router
  - 後續 task 依序 include：participants / events / summaries / bonus_rates / penalty_catalog / reports
"""

from fastapi import APIRouter

from .cycles import router as cycles_router

# 後續 task 加入（T9-T13）：
# from .participants import router as participants_router
# from .events import router as events_router
# from .summaries import router as summaries_router
# from .bonus_rates import router as bonus_rates_router
# from .penalty_catalog import router as penalty_catalog_router
# from .reports import router as reports_router

appraisal_router = APIRouter(prefix="/api/appraisal", tags=["appraisal"])
appraisal_router.include_router(cycles_router)
# 後續 include 其他 router（按功能區塊加入）

__all__ = ["appraisal_router"]
