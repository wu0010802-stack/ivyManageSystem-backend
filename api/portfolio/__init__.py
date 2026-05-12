"""Portfolio 模組 routers 集中地。"""

from api.portfolio.auto_milestone import router as auto_milestone_router
from api.portfolio.measurements import router as measurements_router
from api.portfolio.milestones import router as milestones_router
from api.portfolio.observations import router as observations_router
from api.portfolio.reports import router as growth_reports_router
from api.portfolio.timeline import router as timeline_router

__all__ = [
    "auto_milestone_router",
    "growth_reports_router",
    "measurements_router",
    "milestones_router",
    "observations_router",
    "timeline_router",
]
