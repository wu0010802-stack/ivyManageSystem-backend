"""年終獎金子套件 router 集合。"""

from fastapi import APIRouter

from .cycles import router as cycles_router
from .excel import router as excel_router
from .settlements import router as settlements_router
from .special_bonuses import router as special_bonuses_router

year_end_router = APIRouter(prefix="/api/year_end", tags=["year_end"])
year_end_router.include_router(cycles_router)
year_end_router.include_router(settlements_router)
year_end_router.include_router(special_bonuses_router)
year_end_router.include_router(excel_router)

__all__ = ["year_end_router"]
