"""
api/activity/courses.py — 課程管理端點（5 個）
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import get_session, ActivityCourse, RegistrationCourse, ActivityRegistration
from sqlalchemy import func
from services.activity_service import activity_service
from utils.auth import require_permission
from utils.permissions import Permission

from ._shared import (
    CourseCreate, CourseUpdate,
    _not_found, _duplicate_name,
    _invalidate_activity_dashboard_caches,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/courses")
async def get_courses(
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得課程列表（含報名統計，支援分頁）"""
    session = get_session()
    try:
        q = session.query(ActivityCourse).filter(ActivityCourse.is_active.is_(True))
        total = q.count()
        courses = q.order_by(ActivityCourse.id).offset(skip).limit(limit).all()

        course_ids = [c.id for c in courses]
        enrolled_map: dict[int, int] = {}
        waitlist_map: dict[int, int] = {}
        if course_ids:
            count_rows = (
                session.query(
                    RegistrationCourse.course_id,
                    RegistrationCourse.status,
                    func.count(RegistrationCourse.id),
                )
                .join(ActivityRegistration, RegistrationCourse.registration_id == ActivityRegistration.id)
                .filter(
                    RegistrationCourse.course_id.in_(course_ids),
                    ActivityRegistration.is_active.is_(True),
                    RegistrationCourse.status.in_(["enrolled", "waitlist"]),
                )
                .group_by(RegistrationCourse.course_id, RegistrationCourse.status)
                .all()
            )
            for course_id, status, cnt in count_rows:
                if status == "enrolled":
                    enrolled_map[course_id] = cnt
                else:
                    waitlist_map[course_id] = cnt

        items = []
        for c in courses:
            enrolled = enrolled_map.get(c.id, 0)
            waitlist = waitlist_map.get(c.id, 0)
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
        return {"courses": items, "total": total, "skip": skip, "limit": limit}
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
            raise _not_found("課程")
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
            raise _duplicate_name("課程")

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
        _invalidate_activity_dashboard_caches(session)
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
            raise _not_found("課程")

        if body.name and body.name != course.name:
            dup = session.query(ActivityCourse).filter(
                ActivityCourse.name == body.name,
                ActivityCourse.id != course_id,
            ).first()
            if dup:
                raise _duplicate_name("課程")

        update_data = body.model_dump(exclude_unset=True)
        for k, v in update_data.items():
            setattr(course, k, v)

        session.commit()
        _invalidate_activity_dashboard_caches(session)
        return {"message": "課程更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/courses/{course_id}/waitlist")
async def get_course_waitlist(
    course_id: int,
    current_user: dict = Depends(require_permission(Permission.ACTIVITY_READ)),
):
    """取得課程候補名單（按報名序排列）"""
    session = get_session()
    try:
        course = session.query(ActivityCourse).filter(
            ActivityCourse.id == course_id,
            ActivityCourse.is_active.is_(True),
        ).first()
        if not course:
            raise _not_found("課程")
        rows = (
            session.query(
                RegistrationCourse.id.label("course_record_id"),
                RegistrationCourse.registration_id,
                ActivityRegistration.student_name,
                ActivityRegistration.class_name,
            )
            .join(ActivityRegistration,
                  RegistrationCourse.registration_id == ActivityRegistration.id)
            .filter(
                RegistrationCourse.course_id == course_id,
                RegistrationCourse.status == "waitlist",
                ActivityRegistration.is_active.is_(True),
            )
            .order_by(RegistrationCourse.id.asc())
            .all()
        )
        return {
            "course_id": course_id,
            "course_name": course.name,
            "items": [
                {
                    "waitlist_position": idx + 1,
                    "course_record_id": r.course_record_id,
                    "registration_id": r.registration_id,
                    "student_name": r.student_name,
                    "class_name": r.class_name,
                }
                for idx, r in enumerate(rows)
            ],
        }
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
            raise _not_found("課程")

        count = activity_service.count_active_course_registrations(session, course_id)
        if count > 0:
            raise HTTPException(
                status_code=409,
                detail=f"無法刪除：此課程有 {count} 筆報名記錄",
            )

        course.is_active = False
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        return {"message": "課程已停用"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
