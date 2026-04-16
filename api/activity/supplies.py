"""
api/activity/supplies.py — 用品管理端點（4 個）
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import get_session, ActivitySupply
from utils.auth import require_staff_permission
from utils.permissions import Permission

from ._shared import (
    SupplyCreate, SupplyUpdate,
    _not_found, _duplicate_name,
    _invalidate_activity_dashboard_caches,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/supplies")
async def get_supplies(
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得用品列表（支援分頁）"""
    session = get_session()
    try:
        q = session.query(ActivitySupply).filter(ActivitySupply.is_active.is_(True))
        total = q.count()
        supplies = q.order_by(ActivitySupply.id).offset(skip).limit(limit).all()
        return {
            "supplies": [
                {"id": s.id, "name": s.name, "price": s.price}
                for s in supplies
            ],
            "total": total,
            "skip": skip,
            "limit": limit,
        }
    finally:
        session.close()


@router.post("/supplies", status_code=201)
async def create_supply(
    body: SupplyCreate,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """新增用品"""
    session = get_session()
    try:
        existing = session.query(ActivitySupply).filter(
            ActivitySupply.name == body.name
        ).first()
        if existing:
            raise _duplicate_name("用品")

        supply = ActivitySupply(name=body.name, price=body.price)
        session.add(supply)
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        return {"message": "用品新增成功", "id": supply.id}
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
        supply = session.query(ActivitySupply).filter(
            ActivitySupply.id == supply_id,
            ActivitySupply.is_active.is_(True),
        ).first()
        if not supply:
            raise _not_found("用品")

        if body.name and body.name != supply.name:
            dup = session.query(ActivitySupply).filter(
                ActivitySupply.name == body.name,
                ActivitySupply.id != supply_id,
            ).first()
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
        supply = session.query(ActivitySupply).filter(
            ActivitySupply.id == supply_id,
            ActivitySupply.is_active.is_(True),
        ).first()
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
