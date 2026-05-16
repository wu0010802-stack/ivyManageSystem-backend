"""家長端 FAQ 助手 endpoints（前綴 /assistant）。

只提供唯讀 GET /faq，回傳全部 FAQ 內容（含分類）。所有家長皆可存取，
但仍掛 require_parent_role 以保持與其他 parent_portal 端點一致。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from schemas.parent_assistant import FaqResponse
from services.parent_assistant_service import ParentAssistantService
from utils.auth import require_parent_role

router = APIRouter(prefix="/assistant", tags=["parent-assistant"])


@router.get("/faq", response_model=FaqResponse)
def get_faq(
    response: Response,
    current_user: dict = Depends(require_parent_role()),
) -> FaqResponse:
    response.headers["Cache-Control"] = "private, max-age=300"
    return FaqResponse.model_validate(ParentAssistantService.get_faq())
