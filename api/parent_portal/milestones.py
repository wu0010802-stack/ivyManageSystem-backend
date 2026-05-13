"""api/parent_portal/milestones.py — 家長端里程碑 read + react/ack.

Endpoints:
- GET  /api/parent/milestones?student_id=&limit=
- POST /api/parent/milestones/{milestone_id}/acknowledge?student_id=
- POST /api/parent/milestones/{milestone_id}/react?student_id=
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from models.database import Guardian, StudentMilestone, get_session
from models.portfolio import MILESTONE_REACTIONS
from utils.auth import require_parent_role
from utils.errors import raise_safe_500

from ._shared import _assert_student_owned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/milestones", tags=["parent-milestones"])


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
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "parent_acknowledged_at": (
            m.parent_acknowledged_at.isoformat() if m.parent_acknowledged_at else None
        ),
        "parent_reaction": m.parent_reaction,
    }


@router.get("")
async def parent_list_milestones(
    student_id: int = Query(...),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_parent_role()),
) -> dict:
    try:
        session = get_session()
        try:
            user_id = current_user["user_id"]
            _assert_student_owned(session, user_id, student_id)
            rows = (
                session.query(StudentMilestone)
                .filter(
                    StudentMilestone.student_id == student_id,
                    StudentMilestone.deleted_at.is_(None),
                )
                .order_by(StudentMilestone.achieved_on.desc())
                .limit(limit)
                .all()
            )
            return {"items": [_milestone_to_dict(r) for r in rows]}
        finally:
            session.close()
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="家長端查詢里程碑失敗")


class ReactPayload(BaseModel):
    reaction: str = Field(..., description="like / love / celebrate")


@router.post("/{milestone_id}/react")
async def parent_react(
    milestone_id: int,
    payload: ReactPayload,
    student_id: int = Query(...),
    current_user: dict = Depends(require_parent_role()),
) -> dict:
    if payload.reaction not in MILESTONE_REACTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"reaction 必須是 {list(MILESTONE_REACTIONS)} 之一",
        )
    try:
        session = get_session()
        try:
            user_id = current_user["user_id"]
            _assert_student_owned(session, user_id, student_id)
            # F-V6-04：with_for_update 鎖 milestone row；同學生兩位 guardian 並發
            # react 時避免 parent_acknowledged_by attribution 被後贏者覆蓋
            m = (
                session.query(StudentMilestone)
                .filter_by(id=milestone_id, student_id=student_id)
                .filter(StudentMilestone.deleted_at.is_(None))
                .with_for_update()
                .first()
            )
            if not m:
                raise HTTPException(status_code=404, detail="里程碑不存在")
            m.parent_reaction = payload.reaction
            # 第一次 react 也算 ack（row lock 下重新判 acknowledged_at 仍 None 才寫）
            if m.parent_acknowledged_at is None:
                m.parent_acknowledged_at = datetime.utcnow()
                g = (
                    session.query(Guardian)
                    .filter_by(user_id=user_id, student_id=student_id)
                    .filter(Guardian.deleted_at.is_(None))
                    .first()
                )
                if g:
                    m.parent_acknowledged_by = g.id
            session.commit()
            return _milestone_to_dict(m)
        finally:
            session.close()
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="家長端 reaction 失敗")


@router.post("/{milestone_id}/acknowledge")
async def parent_acknowledge(
    milestone_id: int,
    student_id: int = Query(...),
    current_user: dict = Depends(require_parent_role()),
) -> dict:
    """純標記已看過；不改 reaction."""
    try:
        session = get_session()
        try:
            user_id = current_user["user_id"]
            _assert_student_owned(session, user_id, student_id)
            # F-V6-04：with_for_update 鎖 row；同學生兩位 guardian 並發 ack 不會
            # 重複寫 parent_acknowledged_at（first-ack-wins）與覆蓋 acknowledged_by
            m = (
                session.query(StudentMilestone)
                .filter_by(id=milestone_id, student_id=student_id)
                .filter(StudentMilestone.deleted_at.is_(None))
                .with_for_update()
                .first()
            )
            if not m:
                raise HTTPException(status_code=404, detail="里程碑不存在")
            if m.parent_acknowledged_at is None:
                m.parent_acknowledged_at = datetime.utcnow()
                g = (
                    session.query(Guardian)
                    .filter_by(user_id=user_id, student_id=student_id)
                    .filter(Guardian.deleted_at.is_(None))
                    .first()
                )
                if g:
                    m.parent_acknowledged_by = g.id
                session.commit()
            return _milestone_to_dict(m)
        finally:
            session.close()
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="家長端確認里程碑失敗")
