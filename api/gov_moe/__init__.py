"""MOE reporting module — government reporting (Phase 1+)."""

from fastapi import APIRouter

from api.gov_moe import disability_documents, dashboard
from api.gov_moe import certificates as _certificates_module

router = APIRouter(prefix="/gov-moe", tags=["gov_moe"])
router.include_router(disability_documents.router)
router.include_router(dashboard.router)
router.include_router(_certificates_module.router)
