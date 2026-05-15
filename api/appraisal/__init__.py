"""api/appraisal 子套件：考核系統 router 彙整。

M1 重構：所有子 router（cycles/participants/score_items/summaries/bonus_rates/
score_item_catalog/reports）已移除，由 M4 依新 schema 重寫。本檔保留空殼 router
以維持 main.py 的 `app.include_router(appraisal_router)` 不需動。
"""

from fastapi import APIRouter

appraisal_router = APIRouter(prefix="/api/appraisal", tags=["appraisal"])

__all__ = ["appraisal_router"]
