"""api/portal/contact_book_templates.py — 聯絡簿範本 CRUD

範本兩層：
- personal：教師個人私有（owner_user_id 必填）
- shared：園所共用（需 PORTFOLIO_PUBLISH 權限才能建立 / 編輯 / 刪除）

端點：
- GET    /api/portal/contact-book/templates                列出可見範本
- POST   /api/portal/contact-book/templates                建立個人範本（無 PORTFOLIO_PUBLISH 權限時 scope 強制為 personal）
- PATCH  /api/portal/contact-book/templates/{id}           編輯
- DELETE /api/portal/contact-book/templates/{id}           軟封存
- POST   /api/portal/contact-book/templates/{id}/promote   個人 → 園所共用
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_

from models.database import get_session
from models.contact_book import ContactBookTemplate
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/contact-book/templates", tags=["portal-contact-book-templates"]
)


# ── Pydantic ──────────────────────────────────────────────────────────────


class TemplateFields(BaseModel):
    """範本可填欄位（與 ContactBookEntryFields 同子集，皆 optional）。"""

    mood: Optional[str] = Field(default=None, max_length=20)
    meal_lunch: Optional[int] = Field(default=None, ge=0, le=3)
    meal_snack: Optional[int] = Field(default=None, ge=0, le=3)
    nap_minutes: Optional[int] = Field(default=None, ge=0, le=600)
    bowel: Optional[str] = Field(default=None, max_length=20)
    temperature_c: Optional[float] = Field(default=None, ge=30, le=45)
    teacher_note: Optional[str] = Field(default=None, max_length=2000)
    learning_highlight: Optional[str] = Field(default=None, max_length=2000)


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    scope: str = Field(default="personal", pattern=r"^(personal|shared)$")
    classroom_id: Optional[int] = Field(default=None, gt=0)
    fields: TemplateFields


class TemplateUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    classroom_id: Optional[int] = Field(default=None, gt=0)
    fields: Optional[TemplateFields] = None


# ── Helpers ───────────────────────────────────────────────────────────────


def _has_publish_permission(current_user: dict) -> bool:
    perms = int(current_user.get("permissions", 0) or 0)
    if perms == -1 or perms < 0:  # admin（permissions=-1）
        return True
    return bool(perms & int(Permission.PORTFOLIO_PUBLISH.value))


def _template_to_dict(tpl: ContactBookTemplate) -> dict:
    return {
        "id": tpl.id,
        "name": tpl.name,
        "scope": tpl.scope,
        "owner_user_id": tpl.owner_user_id,
        "classroom_id": tpl.classroom_id,
        "fields": tpl.fields or {},
        "is_archived": bool(tpl.is_archived),
        "created_at": tpl.created_at.isoformat() if tpl.created_at else None,
        "updated_at": tpl.updated_at.isoformat() if tpl.updated_at else None,
    }


def _assert_can_modify(template: ContactBookTemplate, current_user: dict) -> None:
    """確認當前使用者可編輯/刪除此範本：
    - personal：必須是 owner
    - shared：必須有 PORTFOLIO_PUBLISH（管理員 / 主管）
    """
    user_id = current_user["user_id"]
    if template.scope == "shared":
        if not _has_publish_permission(current_user):
            raise HTTPException(status_code=403, detail="無權編輯園所共用範本")
        return
    # personal
    if template.owner_user_id != user_id and not _has_publish_permission(current_user):
        raise HTTPException(status_code=403, detail="僅可操作自己的範本")


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("")
def list_templates(
    include_archived: bool = Query(False),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
):
    """列出對當前教師可見的範本：自己的 personal + 全部 shared。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        q = session.query(ContactBookTemplate).filter(
            or_(
                ContactBookTemplate.scope == "shared",
                ContactBookTemplate.owner_user_id == user_id,
            )
        )
        if not include_archived:
            q = q.filter(ContactBookTemplate.is_archived.is_(False))
        templates = q.order_by(
            ContactBookTemplate.scope.desc(),  # 'shared' 排 'personal' 前
            ContactBookTemplate.updated_at.desc(),
        ).all()
        return {"items": [_template_to_dict(t) for t in templates]}
    finally:
        session.close()


@router.post("", status_code=201)
def create_template(
    payload: TemplateCreate,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
):
    """建立範本。建立 shared 範本需 PORTFOLIO_PUBLISH。"""
    user_id = current_user["user_id"]
    if payload.scope == "shared" and not _has_publish_permission(current_user):
        raise HTTPException(status_code=403, detail="無權建立園所共用範本")

    session = get_session()
    try:
        tpl = ContactBookTemplate(
            name=payload.name.strip(),
            scope=payload.scope,
            owner_user_id=user_id if payload.scope == "personal" else None,
            classroom_id=payload.classroom_id,
            fields=payload.fields.model_dump(exclude_none=False),
            is_archived=False,
        )
        session.add(tpl)
        session.commit()
        session.refresh(tpl)

        request.state.audit_entity_id = str(tpl.id)
        request.state.audit_summary = (
            f"建立聯絡簿範本：scope={tpl.scope} name={tpl.name} id={tpl.id}"
        )
        return _template_to_dict(tpl)
    finally:
        session.close()


@router.patch("/{template_id}")
def update_template(
    template_id: int,
    payload: TemplateUpdate,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
):
    session = get_session()
    try:
        tpl = (
            session.query(ContactBookTemplate)
            .filter(ContactBookTemplate.id == template_id)
            .first()
        )
        if not tpl or tpl.is_archived:
            raise HTTPException(status_code=404, detail="範本不存在")
        _assert_can_modify(tpl, current_user)

        if payload.name is not None:
            tpl.name = payload.name.strip()
        if payload.classroom_id is not None:
            tpl.classroom_id = payload.classroom_id
        if payload.fields is not None:
            tpl.fields = payload.fields.model_dump(exclude_none=False)
        tpl.updated_at = datetime.now()
        session.commit()
        session.refresh(tpl)

        request.state.audit_entity_id = str(tpl.id)
        request.state.audit_summary = f"編輯聯絡簿範本：id={tpl.id} scope={tpl.scope}"
        return _template_to_dict(tpl)
    finally:
        session.close()


@router.delete("/{template_id}")
def delete_template(
    template_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
):
    """軟封存範本（is_archived=True）。"""
    session = get_session()
    try:
        tpl = (
            session.query(ContactBookTemplate)
            .filter(ContactBookTemplate.id == template_id)
            .first()
        )
        if not tpl or tpl.is_archived:
            raise HTTPException(status_code=404, detail="範本不存在")
        _assert_can_modify(tpl, current_user)

        tpl.is_archived = True
        tpl.updated_at = datetime.now()
        session.commit()

        request.state.audit_entity_id = str(tpl.id)
        request.state.audit_summary = f"封存聯絡簿範本：id={tpl.id} scope={tpl.scope}"
        return {"message": "已封存"}
    finally:
        session.close()


@router.post("/{template_id}/promote")
def promote_to_shared(
    template_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_PUBLISH)),
):
    """把個人範本升級為園所共用（需 PORTFOLIO_PUBLISH 權限）。"""
    session = get_session()
    try:
        tpl = (
            session.query(ContactBookTemplate)
            .filter(ContactBookTemplate.id == template_id)
            .first()
        )
        if not tpl or tpl.is_archived:
            raise HTTPException(status_code=404, detail="範本不存在")
        if tpl.scope == "shared":
            raise HTTPException(status_code=400, detail="此範本已是園所共用")

        tpl.scope = "shared"
        tpl.owner_user_id = None
        tpl.updated_at = datetime.now()
        session.commit()
        session.refresh(tpl)

        request.state.audit_entity_id = str(tpl.id)
        request.state.audit_summary = f"提升為共用範本：id={tpl.id}"
        return _template_to_dict(tpl)
    finally:
        session.close()
