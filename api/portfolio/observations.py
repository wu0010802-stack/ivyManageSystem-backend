"""
Portfolio Observations router — 學生日常正向觀察紀錄

路由：
- GET    /api/students/{student_id}/observations           查詢某學生觀察紀錄
- POST   /api/students/{student_id}/observations           新增觀察
- PATCH  /api/students/{student_id}/observations/{obs_id}  編輯觀察
- DELETE /api/students/{student_id}/observations/{obs_id}  軟刪除（其附件一併軟刪）

權限：
- READ  需 PORTFOLIO_READ
- WRITE 需 PORTFOLIO_WRITE

班級 scope：
- teacher 僅可看自己班；非自己班的學生回 403（經 utils/portfolio_access）
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from models.database import (
    Attachment,
    StudentObservation,
    session_scope,
)
from models.portfolio import (
    ATTACHMENT_OWNER_OBSERVATION,
    OBSERVATION_DOMAINS,
)
from utils.auth import require_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/students", tags=["portfolio-observations"])


# ── Pydantic schemas ────────────────────────────────────────────────────


class ObservationCreate(BaseModel):
    observation_date: date
    narrative: str = Field(..., min_length=1, max_length=5000)
    domain: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    is_highlight: bool = False

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if v not in OBSERVATION_DOMAINS:
            raise ValueError(f"domain 必須是以下之一：{', '.join(OBSERVATION_DOMAINS)}")
        return v


class ObservationUpdate(BaseModel):
    observation_date: Optional[date] = None
    narrative: Optional[str] = Field(default=None, min_length=1, max_length=5000)
    domain: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    is_highlight: Optional[bool] = None

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if v not in OBSERVATION_DOMAINS:
            raise ValueError(f"domain 必須是以下之一：{', '.join(OBSERVATION_DOMAINS)}")
        return v


# ── Helpers ──────────────────────────────────────────────────────────────


def _attachments_for_owner(session, owner_type: str, owner_id: int) -> list[dict]:
    """取得一筆 owner 的未刪除附件列表（含三組 URL）。"""
    from api.attachments import _attachment_to_dict  # 避免循環 import

    rows = (
        session.query(Attachment)
        .filter(
            Attachment.owner_type == owner_type,
            Attachment.owner_id == owner_id,
            Attachment.deleted_at.is_(None),
        )
        .order_by(Attachment.created_at.asc())
        .all()
    )
    return [_attachment_to_dict(a) for a in rows]


def _attachments_by_owner_ids(
    session, owner_type: str, owner_ids: list[int]
) -> dict[int, list[dict]]:
    """批次取得多個 owner 的附件 dict（owner_id → list[dict]）。

    Audit G.P0.4：取代 list 端點對每筆 obs 各跑一次 _attachments_for_owner。
    """
    from api.attachments import _attachment_to_dict  # 避免循環 import

    if not owner_ids:
        return {}
    rows = (
        session.query(Attachment)
        .filter(
            Attachment.owner_type == owner_type,
            Attachment.owner_id.in_(owner_ids),
            Attachment.deleted_at.is_(None),
        )
        .order_by(Attachment.owner_id.asc(), Attachment.created_at.asc())
        .all()
    )
    out: dict[int, list[dict]] = {oid: [] for oid in owner_ids}
    for a in rows:
        out.setdefault(a.owner_id, []).append(_attachment_to_dict(a))
    return out


def _observation_to_dict(
    obs: StudentObservation,
    session=None,
    include_attachments: bool = True,
    *,
    attachments_map: dict[int, list[dict]] | None = None,
) -> dict:
    """單筆 observation → dict。

    attachments_map: 若 list 端點已批次預載，傳入 {obs_id: [att_dict, ...]} 即可避免
    這裡再跑單筆 query（audit G.P0.4）。未供預載時 fall back 到原 single query 路徑。
    """
    base = {
        "id": obs.id,
        "student_id": obs.student_id,
        "observation_date": (
            obs.observation_date.isoformat() if obs.observation_date else None
        ),
        "domain": obs.domain,
        "narrative": obs.narrative,
        "rating": obs.rating,
        "is_highlight": obs.is_highlight,
        "recorded_by": obs.recorded_by,
        "created_at": obs.created_at.isoformat() if obs.created_at else None,
        "updated_at": obs.updated_at.isoformat() if obs.updated_at else None,
    }
    if include_attachments:
        if attachments_map is not None:
            base["attachments"] = attachments_map.get(obs.id, [])
        elif session is not None:
            base["attachments"] = _attachments_for_owner(
                session, ATTACHMENT_OWNER_OBSERVATION, obs.id
            )
    return base


# ── Routes ───────────────────────────────────────────────────────────────


@router.get("/{student_id}/observations")
async def list_observations(
    student_id: int,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    domain: Optional[str] = Query(None),
    highlight_only: bool = Query(False),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
) -> dict:
    """查詢某學生的觀察紀錄。"""
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)

            query = session.query(StudentObservation).filter(
                StudentObservation.student_id == student_id,
                StudentObservation.deleted_at.is_(None),
            )
            if from_date:
                query = query.filter(StudentObservation.observation_date >= from_date)
            if to_date:
                query = query.filter(StudentObservation.observation_date <= to_date)
            if domain:
                query = query.filter(StudentObservation.domain == domain)
            if highlight_only:
                query = query.filter(StudentObservation.is_highlight.is_(True))

            total = query.count()
            rows = (
                query.order_by(
                    StudentObservation.observation_date.desc(),
                    StudentObservation.id.desc(),
                )
                .offset(skip)
                .limit(limit)
                .all()
            )
            attachments_map = _attachments_by_owner_ids(
                session, ATTACHMENT_OWNER_OBSERVATION, [r.id for r in rows]
            )
            return {
                "total": total,
                "items": [
                    _observation_to_dict(r, attachments_map=attachments_map)
                    for r in rows
                ],
            }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="查詢觀察紀錄失敗")


@router.post("/{student_id}/observations", status_code=201)
async def create_observation(
    student_id: int,
    payload: ObservationCreate,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
) -> dict:
    """新增觀察紀錄。"""
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)

            obs = StudentObservation(
                student_id=student_id,
                observation_date=payload.observation_date,
                narrative=payload.narrative,
                domain=payload.domain,
                rating=payload.rating,
                is_highlight=payload.is_highlight,
                recorded_by=current_user.get("user_id"),
            )
            session.add(obs)
            session.flush()
            session.refresh(obs)

            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"新增觀察：student_id={student_id} "
                f"date={payload.observation_date} "
                f"highlight={payload.is_highlight}"
            )
            logger.info(
                "新增觀察：student_id=%d obs_id=%d highlight=%s operator=%s",
                student_id,
                obs.id,
                obs.is_highlight,
                current_user.get("username"),
            )
            return _observation_to_dict(obs, session=session)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="新增觀察失敗")


@router.patch("/{student_id}/observations/{obs_id}")
async def update_observation(
    student_id: int,
    obs_id: int,
    payload: ObservationUpdate,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
) -> dict:
    """編輯觀察紀錄。"""
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            obs = (
                session.query(StudentObservation)
                .filter(
                    StudentObservation.id == obs_id,
                    StudentObservation.student_id == student_id,
                    StudentObservation.deleted_at.is_(None),
                )
                .first()
            )
            if not obs:
                raise HTTPException(status_code=404, detail="觀察紀錄不存在")

            data = payload.model_dump(exclude_unset=True)
            for field in (
                "observation_date",
                "narrative",
                "domain",
                "rating",
                "is_highlight",
            ):
                if field in data:
                    setattr(obs, field, data[field])
            obs.updated_at = datetime.now()
            session.flush()
            session.refresh(obs)

            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"編輯觀察：obs_id={obs_id} fields={list(data.keys())}"
            )
            logger.info(
                "編輯觀察：obs_id=%d student_id=%d fields=%s operator=%s",
                obs_id,
                student_id,
                list(data.keys()),
                current_user.get("username"),
            )
            return _observation_to_dict(obs, session=session)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="編輯觀察失敗")


@router.delete("/{student_id}/observations/{obs_id}")
async def delete_observation(
    student_id: int,
    obs_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
) -> dict:
    """軟刪除觀察紀錄；其附件一併軟刪。"""
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            obs = (
                session.query(StudentObservation)
                .filter(
                    StudentObservation.id == obs_id,
                    StudentObservation.student_id == student_id,
                    StudentObservation.deleted_at.is_(None),
                )
                .first()
            )
            if not obs:
                raise HTTPException(status_code=404, detail="觀察紀錄不存在")

            now = datetime.now()
            obs.deleted_at = now
            # Cascade：未刪除的附件一併軟刪
            session.query(Attachment).filter(
                Attachment.owner_type == ATTACHMENT_OWNER_OBSERVATION,
                Attachment.owner_id == obs_id,
                Attachment.deleted_at.is_(None),
            ).update({Attachment.deleted_at: now}, synchronize_session=False)

            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = f"軟刪除觀察：obs_id={obs_id}"
            logger.info(
                "軟刪除觀察：obs_id=%d student_id=%d operator=%s",
                obs_id,
                student_id,
                current_user.get("username"),
            )
            return {"message": "刪除成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="刪除觀察失敗")
