"""
api/activity/supplies.py — 用品管理端點（4 個）
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import get_session, ActivitySupply
from utils.academic import resolve_academic_term_filters
from utils.auth import require_staff_permission
from utils.permissions import Permission

from ._shared import (
    SupplyCreate,
    SupplyUpdate,
    _not_found,
    _duplicate_name,
    _invalidate_activity_dashboard_caches,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/supplies")
async def get_supplies(
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得用品列表（支援分頁、學期篩選）"""
    session = get_session()
    try:
        sy, sem = resolve_academic_term_filters(school_year, semester)
        q = session.query(ActivitySupply).filter(
            ActivitySupply.is_active.is_(True),
            ActivitySupply.school_year == sy,
            ActivitySupply.semester == sem,
        )
        total = q.count()
        supplies = q.order_by(ActivitySupply.id).offset(skip).limit(limit).all()
        return {
            "supplies": [
                {
                    "id": s.id,
                    "name": s.name,
                    "price": s.price,
                    "school_year": s.school_year,
                    "semester": s.semester,
                }
                for s in supplies
            ],
            "total": total,
            "skip": skip,
            "limit": limit,
            "school_year": sy,
            "semester": sem,
        }
    finally:
        session.close()


@router.post("/supplies", status_code=201)
async def create_supply(
    body: SupplyCreate,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """新增用品（同學期內名稱唯一）"""
    session = get_session()
    try:
        sy, sem = resolve_academic_term_filters(body.school_year, body.semester)
        existing = (
            session.query(ActivitySupply)
            .filter(
                ActivitySupply.name == body.name,
                ActivitySupply.school_year == sy,
                ActivitySupply.semester == sem,
                ActivitySupply.is_active.is_(True),
            )
            .first()
        )
        if existing:
            raise _duplicate_name("用品")

        supply = ActivitySupply(
            name=body.name,
            price=body.price,
            school_year=sy,
            semester=sem,
        )
        session.add(supply)
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        return {
            "message": "用品新增成功",
            "id": supply.id,
            "school_year": sy,
            "semester": sem,
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/supplies/{supply_id}")
async def update_supply(
    supply_id: int,
    body: SupplyUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """更新用品"""
    session = get_session()
    try:
        supply = (
            session.query(ActivitySupply)
            .filter(
                ActivitySupply.id == supply_id,
                ActivitySupply.is_active.is_(True),
            )
            .first()
        )
        if not supply:
            raise _not_found("用品")

        if body.name and body.name != supply.name:
            dup = (
                session.query(ActivitySupply)
                .filter(
                    ActivitySupply.name == body.name,
                    ActivitySupply.id != supply_id,
                )
                .first()
            )
            if dup:
                raise _duplicate_name("用品")

        update_data = body.model_dump(exclude_unset=True)
        for k, v in update_data.items():
            setattr(supply, k, v)

        session.commit()
        _invalidate_activity_dashboard_caches(session)
        return {"message": "用品更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/supplies/{supply_id}")
async def delete_supply(
    supply_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """停用用品"""
    session = get_session()
    try:
        supply = (
            session.query(ActivitySupply)
            .filter(
                ActivitySupply.id == supply_id,
                ActivitySupply.is_active.is_(True),
            )
            .first()
        )
        if not supply:
            raise _not_found("用品")

        supply.is_active = False
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        return {"message": "用品已停用"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
