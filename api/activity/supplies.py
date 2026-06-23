"""
api/activity/supplies.py — 用品管理端點（4 個）
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models.database import (
    get_session,
    ActivitySupply,
    ActivityRegistration,
    RegistrationSupply,
)
from utils.academic import resolve_academic_term_filters
from utils.auth import require_staff_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission

from ._shared import (
    SupplyCreate,
    SupplyUpdate,
    _not_found,
    _duplicate_name,
    _invalidate_activity_dashboard_caches,
    require_approve_for_high_price,
)

from schemas.activity_admin import (
    SupplyCreateResultOut,
    SupplyListOut,
)
from schemas._common import DeleteResultOut

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/supplies", response_model=SupplyListOut)
def get_supplies(
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


@router.post("/supplies", status_code=201, response_model=SupplyCreateResultOut)
def create_supply(
    body: SupplyCreate,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """新增用品（同學期內名稱唯一）"""
    require_approve_for_high_price(
        body.price, current_user, label=f"用品「{body.name}」單價"
    )
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
        # 並發同名：兩請求 SELECT 都查不到 → 都 add，後到者撞 partial unique index
        # `uq_activity_supply_name_term`。捕 IntegrityError 轉乾淨 400（與序列同名
        # 走 L105 早退一致），避免落入 generic except → raise_safe_500（500）。
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise _duplicate_name("用品")
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
        raise_safe_500(e)
    finally:
        session.close()


@router.put("/supplies/{supply_id}", response_model=DeleteResultOut)
def update_supply(
    supply_id: int,
    body: SupplyUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """更新用品"""
    if body.price is not None:
        require_approve_for_high_price(
            body.price, current_user, label=f"用品 #{supply_id} 新單價"
        )
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
            # 與 partial unique index (name, school_year, semester)
            # WHERE is_active 對齊；跨學期同名允許，不該在此誤報衝突
            dup = (
                session.query(ActivitySupply)
                .filter(
                    ActivitySupply.name == body.name,
                    ActivitySupply.id != supply_id,
                    ActivitySupply.school_year == supply.school_year,
                    ActivitySupply.semester == supply.semester,
                    ActivitySupply.is_active.is_(True),
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
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/supplies/{supply_id}", response_model=DeleteResultOut)
def delete_supply(
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

        # 仍被在籍（active）報名引用時不可停用：公開查詢仍會回該用品、前端原樣送回，
        # 但公開更新只接受 active 用品 → 家長任何存檔都會 400 被卡住。改在停用點 fail-fast，
        # 提示使用中筆數，須先處理（移除/改選）才能停用。inactive（軟刪）報名不算引用。
        in_use_count = (
            session.query(func.count(RegistrationSupply.id))
            .join(
                ActivityRegistration,
                RegistrationSupply.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationSupply.supply_id == supply_id,
                ActivityRegistration.is_active.is_(True),
            )
            .scalar()
            or 0
        )
        if in_use_count > 0:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"此用品仍被 {in_use_count} 筆有效報名選用，無法停用。"
                    f"請先於這些報名中移除此用品後再停用"
                ),
            )

        supply.is_active = False
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        return {"message": "用品已停用"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
