"""
api/activity.py — 課後才藝報名系統管理後台 API
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from models.database import (
    get_session, Classroom,
    ActivityCourse, ActivitySupply, ActivityRegistration,
    RegistrationCourse, RegistrationSupply,
    ParentInquiry, RegistrationChange, ActivityRegistrationSettings,
)
from services.activity_service import activity_service
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/activity", tags=["activity"])


# ============================================================ #
# Pydantic Models
# ============================================================ #

class CourseCreate(BaseModel):
    name: str
    price: int
    sessions: Optional[int] = None
    capacity: int = 30
    video_url: Optional[str] = None
    allow_waitlist: bool = True
    description: Optional[str] = None


class CourseUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[int] = None
    sessions: Optional[int] = None
    capacity: Optional[int] = None
    video_url: Optional[str] = None
    allow_waitlist: Optional[bool] = None
    description: Optional[str] = None


class SupplyCreate(BaseModel):
    name: str
    price: int


class SupplyUpdate(BaseModel):
    name: Optional[str] = None
    price: Optional[int] = None


class PaymentUpdate(BaseModel):
    is_paid: bool


class RemarkUpdate(BaseModel):
    remark: str


class RegistrationTimeSettings(BaseModel):
    is_open: bool
    open_at: Optional[str] = None
    close_at: Optional[str] = None


class PublicCourseItem(BaseModel):
    name: str
    price: str  # 相容保留，後端實際以 DB 價格為準


class PublicSupplyItem(BaseModel):
    name: str
    price: str  # 相容保留，後端實際以 DB 價格為準


class PublicInquiryPayload(BaseModel):
    name: str
    phone: str
    question: str


class PublicRegistrationPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    birthday: str
    class_: str = Field(..., alias="class")
    courses: list[PublicCourseItem]
    supplies: list[PublicSupplyItem] = []


def _get_active_classroom(session, classroom_name: str):
    """依名稱取得啟用中的班級。"""
    return session.query(Classroom).filter(
        Classroom.name == classroom_name.strip(),
        Classroom.is_active.is_(True),
    ).first()


# ============================================================ #
# 統計儀表板
# ============================================================ #

@router.get("/stats")
async def get_stats(
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得儀表板統計資料"""
    session = get_session()
    try:
        return activity_service.get_stats(session)
    finally:
        session.close()


@router.get("/dashboard-table")
async def get_dashboard_table(
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得儀表板統計表格資料"""
    session = get_session()
    try:
        return activity_service.get_dashboard_table(session)
    finally:
        session.close()


# ============================================================ #
# 報名管理
# ============================================================ #

@router.get("/registrations")
async def get_registrations(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = None,
    payment_status: Optional[str] = None,   # "paid" / "unpaid"
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得報名列表（分頁、搜尋、付款狀態篩選）"""
    session = get_session()
    try:
        q = session.query(ActivityRegistration).filter(
            ActivityRegistration.is_active.is_(True)
        )
        if search:
            like = f"%{search}%"
            q = q.filter(
                (ActivityRegistration.student_name.ilike(like)) |
                (ActivityRegistration.class_name.ilike(like))
            )
        if payment_status == "paid":
            q = q.filter(ActivityRegistration.is_paid.is_(True))
        elif payment_status == "unpaid":
            q = q.filter(ActivityRegistration.is_paid.is_(False))

        total = q.count()
        regs = q.order_by(ActivityRegistration.created_at.desc()).offset(skip).limit(limit).all()

        items = []
        for r in regs:
            course_count = session.query(RegistrationCourse).filter(
                RegistrationCourse.registration_id == r.id
            ).count()
            supply_count = session.query(RegistrationSupply).filter(
                RegistrationSupply.registration_id == r.id
            ).count()

            # 課程摘要
            rc_rows = (
                session.query(RegistrationCourse, ActivityCourse)
                .join(ActivityCourse, RegistrationCourse.course_id == ActivityCourse.id)
                .filter(RegistrationCourse.registration_id == r.id)
                .all()
            )
            course_names = "、".join(
                f"{ac.name}（候補）" if rc.status == "waitlist" else ac.name
                for rc, ac in rc_rows
            )

            items.append({
                "id": r.id,
                "student_name": r.student_name,
                "birthday": r.birthday,
                "class_name": r.class_name,
                "email": r.email,
                "is_paid": r.is_paid,
                "remark": r.remark or "",
                "course_count": course_count,
                "supply_count": supply_count,
                "course_names": course_names,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            })
        return {"items": items, "total": total, "skip": skip, "limit": limit}
    finally:
        session.close()


@router.get("/registrations/{registration_id}")
async def get_registration_detail(
    registration_id: int,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得報名詳情（含課程/用品/修改紀錄）"""
    session = get_session()
    try:
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise HTTPException(status_code=404, detail="找不到報名資料")

        rc_rows = (
            session.query(RegistrationCourse, ActivityCourse)
            .join(ActivityCourse, RegistrationCourse.course_id == ActivityCourse.id)
            .filter(RegistrationCourse.registration_id == registration_id)
            .all()
        )
        courses = [
            {
                "id": rc.id,
                "course_id": ac.id,
                "name": ac.name,
                "price": rc.price_snapshot,
                "status": rc.status,
            }
            for rc, ac in rc_rows
        ]

        rs_rows = (
            session.query(RegistrationSupply, ActivitySupply)
            .join(ActivitySupply, RegistrationSupply.supply_id == ActivitySupply.id)
            .filter(RegistrationSupply.registration_id == registration_id)
            .all()
        )
        supplies = [
            {"id": rs.id, "supply_id": sp.id, "name": sp.name, "price": rs.price_snapshot}
            for rs, sp in rs_rows
        ]

        changes = (
            session.query(RegistrationChange)
            .filter(RegistrationChange.registration_id == registration_id)
            .order_by(RegistrationChange.created_at.desc())
            .limit(20)
            .all()
        )
        change_list = [
            {
                "id": ch.id,
                "change_type": ch.change_type,
                "description": ch.description,
                "changed_by": ch.changed_by,
                "created_at": ch.created_at.isoformat() if ch.created_at else None,
            }
            for ch in changes
        ]

        total_amount = sum(c["price"] for c in courses if c["status"] == "enrolled")
        total_amount += sum(s["price"] for s in supplies)

        return {
            "id": reg.id,
            "student_name": reg.student_name,
            "birthday": reg.birthday,
            "class_name": reg.class_name,
            "email": reg.email,
            "is_paid": reg.is_paid,
            "remark": reg.remark or "",
            "courses": courses,
            "supplies": supplies,
            "changes": change_list,
            "total_amount": total_amount,
            "created_at": reg.created_at.isoformat() if reg.created_at else None,
            "updated_at": reg.updated_at.isoformat() if reg.updated_at else None,
        }
    finally:
        session.close()


@router.put("/registrations/{registration_id}/payment")
async def update_payment(
    registration_id: int,
    body: PaymentUpdate,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """更新付款狀態"""
    session = get_session()
    try:
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise HTTPException(status_code=404, detail="找不到報名資料")

        old_paid = reg.is_paid
        reg.is_paid = body.is_paid

        status_str = "已繳費" if body.is_paid else "未繳費"
        activity_service.log_change(
            session, registration_id, reg.student_name,
            "更新付款狀態", f"付款狀態更新為：{status_str}",
            current_user.get("username", ""),
        )
        session.commit()
        return {"message": f"更新成功，狀態為：{status_str}"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/registrations/{registration_id}/remark")
async def update_remark(
    registration_id: int,
    body: RemarkUpdate,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """更新備註"""
    session = get_session()
    try:
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise HTTPException(status_code=404, detail="找不到報名資料")

        reg.remark = body.remark
        activity_service.log_change(
            session, registration_id, reg.student_name,
            "更新備註", f"備註更新為：{body.remark}",
            current_user.get("username", ""),
        )
        session.commit()
        return {"message": "備註更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/registrations/{registration_id}/waitlist")
async def promote_waitlist(
    registration_id: int,
    course_id: int = Query(...),
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """將候補升為正式報名"""
    session = get_session()
    try:
        activity_service.promote_waitlist(session, registration_id, course_id)

        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == registration_id
        ).first()
        course = session.query(ActivityCourse).filter(ActivityCourse.id == course_id).first()

        activity_service.log_change(
            session, registration_id, reg.student_name if reg else str(registration_id),
            "候補升正式", f"課程「{course.name if course else course_id}」候補升為正式",
            current_user.get("username", ""),
        )
        session.commit()
        return {"message": "成功升為正式報名"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/registrations/{registration_id}")
async def delete_registration(
    registration_id: int,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """軟刪除報名"""
    session = get_session()
    try:
        activity_service.delete_registration(
            session, registration_id,
            current_user.get("username", ""),
        )
        session.commit()
        logger.warning(
            "課後才藝報名已刪除：id=%s operator=%s",
            registration_id, current_user.get("username"),
        )
        return {"message": "報名已刪除"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ============================================================ #
# 課程管理
# ============================================================ #

@router.get("/courses")
async def get_courses(
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得課程列表（含報名統計）"""
    session = get_session()
    try:
        courses = session.query(ActivityCourse).filter(
            ActivityCourse.is_active.is_(True)
        ).order_by(ActivityCourse.id).all()

        items = []
        for c in courses:
            enrolled = activity_service.count_active_course_registrations(
                session, c.id, status="enrolled"
            )
            waitlist = activity_service.count_active_course_registrations(
                session, c.id, status="waitlist"
            )
            capacity = c.capacity if c.capacity is not None else 30
            items.append({
                "id": c.id,
                "name": c.name,
                "price": c.price,
                "sessions": c.sessions,
                "capacity": capacity,
                "video_url": c.video_url or "",
                "allow_waitlist": c.allow_waitlist,
                "description": c.description or "",
                "enrolled": enrolled,
                "waitlist_count": waitlist,
                "remaining": max(0, capacity - enrolled),
            })
        return {"courses": items}
    finally:
        session.close()


@router.get("/courses/{course_id}")
async def get_course_detail(
    course_id: int,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得課程詳情"""
    session = get_session()
    try:
        c = session.query(ActivityCourse).filter(
            ActivityCourse.id == course_id,
            ActivityCourse.is_active.is_(True),
        ).first()
        if not c:
            raise HTTPException(status_code=404, detail="找不到課程")
        return {
            "id": c.id,
            "name": c.name,
            "price": c.price,
            "sessions": c.sessions,
            "capacity": c.capacity,
            "video_url": c.video_url or "",
            "allow_waitlist": c.allow_waitlist,
            "description": c.description or "",
        }
    finally:
        session.close()


@router.post("/courses", status_code=201)
async def create_course(
    body: CourseCreate,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """新增課程"""
    session = get_session()
    try:
        existing = session.query(ActivityCourse).filter(
            ActivityCourse.name == body.name
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="課程名稱已存在")

        course = ActivityCourse(
            name=body.name,
            price=body.price,
            sessions=body.sessions,
            capacity=body.capacity,
            video_url=body.video_url,
            allow_waitlist=body.allow_waitlist,
            description=body.description,
        )
        session.add(course)
        session.commit()
        return {"message": "課程新增成功", "id": course.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/courses/{course_id}")
async def update_course(
    course_id: int,
    body: CourseUpdate,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """更新課程"""
    session = get_session()
    try:
        course = session.query(ActivityCourse).filter(
            ActivityCourse.id == course_id,
            ActivityCourse.is_active.is_(True),
        ).first()
        if not course:
            raise HTTPException(status_code=404, detail="找不到課程")

        if body.name and body.name != course.name:
            dup = session.query(ActivityCourse).filter(
                ActivityCourse.name == body.name,
                ActivityCourse.id != course_id,
            ).first()
            if dup:
                raise HTTPException(status_code=400, detail="課程名稱已被使用")

        update_data = body.dict(exclude_unset=True)
        for k, v in update_data.items():
            setattr(course, k, v)

        session.commit()
        return {"message": "課程更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/courses/{course_id}")
async def delete_course(
    course_id: int,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """停用課程（有報名者回傳 409）"""
    session = get_session()
    try:
        course = session.query(ActivityCourse).filter(
            ActivityCourse.id == course_id,
            ActivityCourse.is_active.is_(True),
        ).first()
        if not course:
            raise HTTPException(status_code=404, detail="找不到課程")

        count = activity_service.count_active_course_registrations(session, course_id)
        if count > 0:
            raise HTTPException(
                status_code=409,
                detail=f"無法刪除：此課程有 {count} 筆報名記錄",
            )

        course.is_active = False
        session.commit()
        return {"message": "課程已停用"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ============================================================ #
# 用品管理
# ============================================================ #

@router.get("/supplies")
async def get_supplies(
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得用品列表"""
    session = get_session()
    try:
        supplies = session.query(ActivitySupply).filter(
            ActivitySupply.is_active.is_(True)
        ).order_by(ActivitySupply.id).all()
        return {
            "supplies": [
                {"id": s.id, "name": s.name, "price": s.price}
                for s in supplies
            ]
        }
    finally:
        session.close()


@router.post("/supplies", status_code=201)
async def create_supply(
    body: SupplyCreate,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """新增用品"""
    session = get_session()
    try:
        existing = session.query(ActivitySupply).filter(
            ActivitySupply.name == body.name
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="用品名稱已存在")

        supply = ActivitySupply(name=body.name, price=body.price)
        session.add(supply)
        session.commit()
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
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """更新用品"""
    session = get_session()
    try:
        supply = session.query(ActivitySupply).filter(
            ActivitySupply.id == supply_id,
            ActivitySupply.is_active.is_(True),
        ).first()
        if not supply:
            raise HTTPException(status_code=404, detail="找不到用品")

        if body.name and body.name != supply.name:
            dup = session.query(ActivitySupply).filter(
                ActivitySupply.name == body.name,
                ActivitySupply.id != supply_id,
            ).first()
            if dup:
                raise HTTPException(status_code=400, detail="用品名稱已被使用")

        update_data = body.dict(exclude_unset=True)
        for k, v in update_data.items():
            setattr(supply, k, v)

        session.commit()
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
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """停用用品"""
    session = get_session()
    try:
        supply = session.query(ActivitySupply).filter(
            ActivitySupply.id == supply_id,
            ActivitySupply.is_active.is_(True),
        ).first()
        if not supply:
            raise HTTPException(status_code=404, detail="找不到用品")

        supply.is_active = False
        session.commit()
        return {"message": "用品已停用"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ============================================================ #
# 家長提問
# ============================================================ #

@router.get("/inquiries")
async def get_inquiries(
    is_read: Optional[bool] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得家長提問列表"""
    session = get_session()
    try:
        q = session.query(ParentInquiry)
        if is_read is not None:
            q = q.filter(ParentInquiry.is_read.is_(is_read))
        total = q.count()
        rows = q.order_by(ParentInquiry.created_at.desc()).offset(skip).limit(limit).all()
        items = [
            {
                "id": r.id,
                "name": r.name,
                "phone": r.phone,
                "question": r.question,
                "is_read": r.is_read,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return {"items": items, "total": total}
    finally:
        session.close()


@router.put("/inquiries/{inquiry_id}/read")
async def mark_inquiry_read(
    inquiry_id: int,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """標記提問為已讀"""
    session = get_session()
    try:
        inquiry = session.query(ParentInquiry).filter(ParentInquiry.id == inquiry_id).first()
        if not inquiry:
            raise HTTPException(status_code=404, detail="找不到提問")
        inquiry.is_read = True
        session.commit()
        return {"message": "已標記為已讀"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/inquiries/{inquiry_id}")
async def delete_inquiry(
    inquiry_id: int,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """刪除提問"""
    session = get_session()
    try:
        inquiry = session.query(ParentInquiry).filter(ParentInquiry.id == inquiry_id).first()
        if not inquiry:
            raise HTTPException(status_code=404, detail="找不到提問")
        session.delete(inquiry)
        session.commit()
        return {"message": "已刪除"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ============================================================ #
# 報名時間設定
# ============================================================ #

@router.get("/settings/registration-time")
async def get_registration_time(
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得報名開放設定（管理後台用，需認證）"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            return {"is_open": False, "open_at": None, "close_at": None}
        return {
            "is_open": settings.is_open,
            "open_at": settings.open_at,
            "close_at": settings.close_at,
        }
    finally:
        session.close()


@router.get("/public/registration-time")
async def get_public_registration_time():
    """公開端點：前台查詢報名開放時間（無需認證）"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            return {"is_open": False, "open_at": None, "close_at": None}
        return {
            "is_open": settings.is_open,
            "open_at": settings.open_at,
            "close_at": settings.close_at,
        }
    finally:
        session.close()


# ============================================================ #
# 公開端點（前台，無需認證）
# ============================================================ #

@router.get("/public/courses")
async def get_public_courses():
    """前台：取得課程列表"""
    session = get_session()
    try:
        courses = session.query(ActivityCourse).filter(
            ActivityCourse.is_active.is_(True)
        ).order_by(ActivityCourse.id).all()
        return [
            {"name": c.name, "price": c.price, "sessions": c.sessions, "frequency": ""}
            for c in courses
        ]
    finally:
        session.close()


@router.get("/public/supplies")
async def get_public_supplies():
    """前台：取得用品列表"""
    session = get_session()
    try:
        supplies = session.query(ActivitySupply).filter(
            ActivitySupply.is_active.is_(True)
        ).order_by(ActivitySupply.id).all()
        return [{"name": s.name, "price": s.price} for s in supplies]
    finally:
        session.close()


@router.get("/public/classes")
async def get_public_classes():
    """前台：取得班級選項"""
    session = get_session()
    try:
        classrooms = (
            session.query(Classroom)
            .filter(Classroom.is_active.is_(True))
            .order_by(Classroom.id)
            .all()
        )
        return [c.name for c in classrooms]
    finally:
        session.close()


@router.get("/public/courses/availability")
async def get_public_courses_availability():
    """前台：取得課程名額狀況"""
    session = get_session()
    try:
        courses = session.query(ActivityCourse).filter(
            ActivityCourse.is_active.is_(True)
        ).all()
        availability = {}
        for course in courses:
            enrolled = activity_service.count_active_course_registrations(
                session, course.id, status="enrolled"
            )
            capacity = course.capacity if course.capacity is not None else 30
            remaining = capacity - enrolled
            if remaining <= 0:
                availability[course.name] = -1 if not course.allow_waitlist else 0
            else:
                availability[course.name] = remaining
        return availability
    finally:
        session.close()


@router.post("/public/register", status_code=201)
async def public_register(body: PublicRegistrationPayload):
    """前台：提交報名表"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if settings:
            if not settings.is_open:
                raise HTTPException(status_code=400, detail="報名尚未開放")
            now_str = datetime.now().isoformat()
            if settings.open_at and now_str < settings.open_at:
                raise HTTPException(status_code=400, detail="報名尚未開始")
            if settings.close_at and now_str > settings.close_at:
                raise HTTPException(status_code=400, detail="報名已截止")

        classroom_name = body.class_.strip()
        classroom = _get_active_classroom(session, classroom_name)
        if not classroom:
            raise HTTPException(status_code=400, detail="班級不存在或已停用")

        reg = ActivityRegistration(
            student_name=body.name,
            birthday=body.birthday,
            class_name=classroom.name,
        )
        session.add(reg)
        session.flush()  # 取得 reg.id

        has_waitlist = False
        for course_item in body.courses:
            course = session.query(ActivityCourse).filter(
                ActivityCourse.name == course_item.name,
                ActivityCourse.is_active.is_(True),
            ).first()
            if not course:
                raise HTTPException(status_code=400, detail=f"找不到課程：{course_item.name}")

            enrolled_count = activity_service.count_active_course_registrations(
                session, course.id, status="enrolled"
            )
            capacity = course.capacity if course.capacity is not None else 30
            remaining = capacity - enrolled_count

            if remaining > 0:
                status = "enrolled"
            elif course.allow_waitlist:
                status = "waitlist"
                has_waitlist = True
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"課程「{course.name}」已額滿且不開放候補",
                )

            rc = RegistrationCourse(
                registration_id=reg.id,
                course_id=course.id,
                status=status,
                price_snapshot=course.price,
            )
            session.add(rc)

        for supply_item in body.supplies:
            supply = session.query(ActivitySupply).filter(
                ActivitySupply.name == supply_item.name,
                ActivitySupply.is_active.is_(True),
            ).first()
            if not supply:
                raise HTTPException(status_code=400, detail=f"找不到用品：{supply_item.name}")

            rs = RegistrationSupply(
                registration_id=reg.id,
                supply_id=supply.id,
                price_snapshot=supply.price,
            )
            session.add(rs)

        session.commit()
        logger.info("新報名提交：id=%s student=%s", reg.id, reg.student_name)

        msg = (
            "報名成功！您有課程進入候補名單，我們會儘快通知您。"
            if has_waitlist
            else "報名成功！感謝您的報名。"
        )
        return {"message": msg, "id": reg.id, "waitlisted": has_waitlist}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logger.error("公開報名失敗：%s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.post("/public/inquiries", status_code=201)
async def public_create_inquiry(body: PublicInquiryPayload):
    """前台：提交家長提問"""
    session = get_session()
    try:
        inquiry = ParentInquiry(
            name=body.name,
            phone=body.phone,
            question=body.question,
        )
        session.add(inquiry)
        session.commit()
        return {"message": "感謝您的提問，我們會儘快回覆您！"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/public/course-videos")
async def get_public_course_videos():
    """前台：取得課程介紹影片 URL"""
    session = get_session()
    try:
        courses = session.query(ActivityCourse).filter(
            ActivityCourse.is_active.is_(True),
            ActivityCourse.video_url.isnot(None),
            ActivityCourse.video_url != "",
        ).all()
        return {c.name: c.video_url for c in courses}
    finally:
        session.close()


class PublicUpdatePayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int
    name: str
    birthday: str
    class_: str = Field(..., alias="class")
    courses: list[PublicCourseItem]
    supplies: list[PublicSupplyItem] = []


@router.get("/public/query")
async def public_query_registration(name: str, birthday: str):
    """前台：依姓名+生日查詢報名資料"""
    session = get_session()
    try:
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.student_name == name,
            ActivityRegistration.birthday == birthday,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise HTTPException(status_code=404, detail="找不到該名幼兒的報名資料")

        rc_rows = (
            session.query(RegistrationCourse, ActivityCourse)
            .join(ActivityCourse, RegistrationCourse.course_id == ActivityCourse.id)
            .filter(RegistrationCourse.registration_id == reg.id)
            .all()
        )
        courses = []
        for rc, ac in rc_rows:
            waitlist_position = None
            if rc.status == "waitlist":
                waitlist_position = (
                    session.query(RegistrationCourse)
                    .join(
                        ActivityRegistration,
                        RegistrationCourse.registration_id == ActivityRegistration.id,
                    )
                    .filter(
                        RegistrationCourse.course_id == ac.id,
                        RegistrationCourse.status == "waitlist",
                        RegistrationCourse.id <= rc.id,
                        ActivityRegistration.is_active.is_(True),
                    )
                    .count()
                )
            courses.append({
                "name": ac.name,
                "price": rc.price_snapshot,
                "status": rc.status,
                "waitlist_position": waitlist_position,
            })

        rs_rows = (
            session.query(RegistrationSupply, ActivitySupply)
            .join(ActivitySupply, RegistrationSupply.supply_id == ActivitySupply.id)
            .filter(RegistrationSupply.registration_id == reg.id)
            .all()
        )
        supplies = [{"name": sp.name, "price": rs.price_snapshot} for rs, sp in rs_rows]

        return {
            "id": reg.id,
            "name": reg.student_name,
            "birthday": reg.birthday,
            "class": reg.class_name,
            "courses": courses,
            "supplies": supplies,
        }
    finally:
        session.close()


@router.post("/public/update")
async def public_update_registration(body: PublicUpdatePayload):
    """前台：依 id 更新報名資料（班級/課程/用品）"""
    session = get_session()
    try:
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == body.id,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise HTTPException(status_code=404, detail="找不到報名資料")

        # 驗證姓名+生日（防止他人竄改）
        if reg.student_name != body.name or reg.birthday != body.birthday:
            raise HTTPException(status_code=403, detail="姓名或生日不符，無法修改")

        classroom = _get_active_classroom(session, body.class_)
        if not classroom:
            raise HTTPException(status_code=400, detail="班級不存在或已停用")
        reg.class_name = classroom.name

        # 刪除舊的課程/用品關聯，重新建立
        session.query(RegistrationCourse).filter(
            RegistrationCourse.registration_id == reg.id
        ).delete()
        session.query(RegistrationSupply).filter(
            RegistrationSupply.registration_id == reg.id
        ).delete()
        session.flush()

        has_waitlist = False
        for course_item in body.courses:
            course = session.query(ActivityCourse).filter(
                ActivityCourse.name == course_item.name,
                ActivityCourse.is_active.is_(True),
            ).first()
            if not course:
                raise HTTPException(status_code=400, detail=f"找不到課程：{course_item.name}")

            enrolled_count = activity_service.count_active_course_registrations(
                session, course.id, status="enrolled"
            )
            capacity = course.capacity if course.capacity is not None else 30
            if enrolled_count < capacity:
                status = "enrolled"
            elif course.allow_waitlist:
                status = "waitlist"
                has_waitlist = True
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"課程「{course.name}」已額滿且不開放候補",
                )
            rc = RegistrationCourse(
                registration_id=reg.id,
                course_id=course.id,
                status=status,
                price_snapshot=course.price,
            )
            session.add(rc)

        for supply_item in body.supplies:
            supply = session.query(ActivitySupply).filter(
                ActivitySupply.name == supply_item.name,
                ActivitySupply.is_active.is_(True),
            ).first()
            if not supply:
                raise HTTPException(status_code=400, detail=f"找不到用品：{supply_item.name}")
            rs = RegistrationSupply(
                registration_id=reg.id,
                supply_id=supply.id,
                price_snapshot=supply.price,
            )
            session.add(rs)

        session.commit()
        logger.info("前台更新報名：id=%s student=%s", reg.id, reg.student_name)
        return {"message": "資料更新成功！"}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logger.error("前台更新報名失敗：%s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.post("/settings/registration-time")
async def update_registration_time(
    body: RegistrationTimeSettings,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_WRITE)),
):
    """更新報名開放設定"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            settings = ActivityRegistrationSettings()
            session.add(settings)

        settings.is_open = body.is_open
        settings.open_at = body.open_at
        settings.close_at = body.close_at
        session.commit()
        return {"message": "報名時間設定已更新"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ============================================================ #
# 修改紀錄
# ============================================================ #

@router.get("/changes")
async def get_changes(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得修改紀錄列表"""
    session = get_session()
    try:
        q = session.query(RegistrationChange)
        total = q.count()
        rows = (
            q.order_by(RegistrationChange.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
        items = [
            {
                "id": r.id,
                "registration_id": r.registration_id,
                "student_name": r.student_name,
                "change_type": r.change_type,
                "description": r.description,
                "changed_by": r.changed_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return {"items": items, "total": total}
    finally:
        session.close()


# ============================================================ #
# 班級選項（從 Classroom 動態取得）
# ============================================================ #

@router.get("/class-options")
async def get_class_options(
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """從 Classroom 表動態取得班級名稱選項"""
    session = get_session()
    try:
        classrooms = (
            session.query(Classroom)
            .filter(Classroom.is_active.is_(True))
            .order_by(Classroom.id)
            .all()
        )
        return {"options": [c.name for c in classrooms]}
    finally:
        session.close()
