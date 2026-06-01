"""Portfolio Milestones router — 學生結構化里程碑.

路由：
- GET    /api/students/{student_id}/milestones
- POST   /api/students/{student_id}/milestones
- PATCH  /api/students/{student_id}/milestones/{m_id}
- DELETE /api/students/{student_id}/milestones/{m_id}

權限：
- READ  需 PORTFOLIO_READ
- WRITE 需 PORTFOLIO_WRITE
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from utils.taipei_time import now_taipei_naive
from utils.taipei_time import today_taipei
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from models.database import StudentMilestone, User, session_scope
from models.portfolio import (
    MILESTONE_SOURCE_MANUAL,
    MILESTONE_TYPES,
)
from utils.audit import write_explicit_audit
from utils.auth import require_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/students", tags=["portfolio-milestones"])


# ── Pydantic models ──────────────────────────────────────────────────────────


class MilestoneCreate(BaseModel):
    milestone_type: str
    achieved_on: date
    title: str = Field(..., max_length=120)
    description: Optional[str] = None
    icon: Optional[str] = Field(default=None, max_length=40)

    @field_validator("milestone_type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        if v not in MILESTONE_TYPES:
            raise ValueError(
                f"milestone_type 必須為以下之一：{', '.join(MILESTONE_TYPES)}"
            )
        return v

    @field_validator("achieved_on")
    @classmethod
    def _no_future(cls, v: date) -> date:
        if v > today_taipei():  
            raise ValueError("achieved_on 不可為未來日期")
        return v


class MilestoneUpdate(BaseModel):
    milestone_type: Optional[str] = None
    achieved_on: Optional[date] = None
    title: Optional[str] = Field(default=None, max_length=120)
    description: Optional[str] = None
    icon: Optional[str] = Field(default=None, max_length=40)

    @field_validator("milestone_type")
    @classmethod
    def _validate_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in MILESTONE_TYPES:
            raise ValueError(
                f"milestone_type 必須為以下之一：{', '.join(MILESTONE_TYPES)}"
            )
        return v

    @field_validator("achieved_on")
    @classmethod
    def _no_future(cls, v: Optional[date]) -> Optional[date]:
        if v is not None and v > today_taipei():  
            raise ValueError("achieved_on 不可為未來日期")
        return v


def _milestone_to_dict(m: StudentMilestone) -> dict:
    return {
        "id": m.id,
        "student_id": m.student_id,
        "milestone_type": m.milestone_type,
        "achieved_on": m.achieved_on.isoformat() if m.achieved_on else None,
        "title": m.title,
        "description": m.description,
        "icon": m.icon,
        "source_type": m.source_type,
        "source_ref_type": m.source_ref_type,
        "source_ref_id": m.source_ref_id,
        "created_by": m.created_by,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        "deleted_at": m.deleted_at.isoformat() if m.deleted_at else None,
        "parent_acknowledged_at": (
            m.parent_acknowledged_at.isoformat() if m.parent_acknowledged_at else None
        ),
        "parent_reaction": m.parent_reaction,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/{student_id}/milestones")
async def list_milestones(
    student_id: int,
    request: Request,
    milestone_type: Optional[str] = Query(None),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id, code=Permission.PORTFOLIO_READ.value)
            query = session.query(StudentMilestone).filter(
                StudentMilestone.student_id == student_id,
                StudentMilestone.deleted_at.is_(None),
            )
            if milestone_type:
                query = query.filter(StudentMilestone.milestone_type == milestone_type)
            if from_date:
                query = query.filter(StudentMilestone.achieved_on >= from_date)
            if to_date:
                query = query.filter(StudentMilestone.achieved_on <= to_date)
            total = query.count()
            rows = (
                query.order_by(
                    StudentMilestone.achieved_on.desc(),
                    StudentMilestone.id.desc(),
                )
                .offset(skip)
                .limit(limit)
                .all()
            )
            write_explicit_audit(
                request,
                action="READ",
                entity_type="portfolio_milestone",
                entity_id=str(student_id),
                summary=f"查詢學生里程碑列表：student_id={student_id} total={total}",
                changes={
                    "milestone_type": milestone_type,
                    "from": from_date.isoformat() if from_date else None,
                    "to": to_date.isoformat() if to_date else None,
                    "total": total,
                    "returned": len(rows),
                },
                dedup=True,
            )
            return {
                "total": total,
                "items": [_milestone_to_dict(r) for r in rows],
            }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="查詢里程碑失敗")


@router.post("/{student_id}/milestones", status_code=201)
async def create_milestone(
    student_id: int,
    payload: MilestoneCreate,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id, code=Permission.PORTFOLIO_WRITE.value)
            # created_by → employees.id；透過 User.employee_id 轉換
            user_id = current_user.get("user_id")
            employee_id: int | None = None
            if user_id is not None:
                u = session.query(User).filter(User.id == user_id).first()
                employee_id = u.employee_id if u else None
            m = StudentMilestone(
                student_id=student_id,
                milestone_type=payload.milestone_type,
                achieved_on=payload.achieved_on,
                title=payload.title,
                description=payload.description,
                icon=payload.icon,
                source_type=MILESTONE_SOURCE_MANUAL,
                created_by=employee_id,
            )
            session.add(m)
            session.flush()
            session.refresh(m)
            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"新增里程碑：student_id={student_id} type={payload.milestone_type}"
            )
            logger.info(
                "新增里程碑：student_id=%d m_id=%d type=%s operator=%s",
                student_id,
                m.id,
                payload.milestone_type,
                current_user.get("username"),
            )
            return _milestone_to_dict(m)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="新增里程碑失敗")


@router.patch("/{student_id}/milestones/{m_id}")
async def update_milestone(
    student_id: int,
    m_id: int,
    payload: MilestoneUpdate,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id, code=Permission.PORTFOLIO_WRITE.value)
            m = (
                session.query(StudentMilestone)
                .filter(
                    StudentMilestone.id == m_id,
                    StudentMilestone.student_id == student_id,
                    StudentMilestone.deleted_at.is_(None),
                )
                .first()
            )
            if not m:
                raise HTTPException(status_code=404, detail="里程碑不存在")
            data = payload.model_dump(exclude_unset=True)
            for key, value in data.items():
                setattr(m, key, value)
            session.flush()
            session.refresh(m)
            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"更新里程碑：student_id={student_id} milestone_id={m_id}"
            )
            return _milestone_to_dict(m)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="更新里程碑失敗")


@router.delete("/{student_id}/milestones/{m_id}", status_code=204)
async def delete_milestone(
    student_id: int,
    m_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
) -> Response:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id, code=Permission.PORTFOLIO_WRITE.value)
            m = (
                session.query(StudentMilestone)
                .filter(
                    StudentMilestone.id == m_id,
                    StudentMilestone.student_id == student_id,
                    StudentMilestone.deleted_at.is_(None),
                )
                .first()
            )
            if not m:
                raise HTTPException(status_code=404, detail="里程碑不存在")
            m.deleted_at = now_taipei_naive()
            session.flush()
            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"刪除里程碑：student_id={student_id} milestone_id={m_id}"
            )
            return Response(status_code=204)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="刪除里程碑失敗")
