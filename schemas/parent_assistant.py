"""家長端 FAQ 助手相關 schemas。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class FaqAction(BaseModel):
    """FAQ 答案附帶的 CTA 動作。"""
    type: Literal["route", "contact_teacher", "external"]
    label: str
    path: Optional[str] = None       # route 用
    url: Optional[str] = None        # external 用


class FaqCategory(BaseModel):
    id: str
    label: str
    icon: str
    color: str


class FaqItem(BaseModel):
    id: str
    category: str
    question: str
    keywords: list[str] = Field(default_factory=list)
    answer: str
    action: Optional[FaqAction] = None


class FaqResponse(BaseModel):
    version: str
    updated_at: str
    categories: list[FaqCategory]
    items: list[FaqItem]
