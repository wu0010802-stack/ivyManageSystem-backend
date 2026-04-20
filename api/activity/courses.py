"""
api/activity/courses.py — 課程管理端點（5 個）
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import (
    get_session,
    ActivityCourse,
    RegistrationCourse,
    ActivityRegistration,
)
from sqlalchemy import func
from services.activity_service import activity_service
from utils.academic import resolve_academic_term_filters, resolve_current_academic_term
from utils.auth import require_staff_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission

from ._shared import (
    CopyCoursesRequest,
    CourseCreate,
    CourseUpdate,
    _not_found,
    _duplicate_name,
    _invalidate_activity_dashboard_caches,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/courses")
async def get_courses(
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得課程列表（含報名統計，支援分頁，依學期過濾）"""
    session = get_session()
    try:
        sy, sem = resolve_academic_term_filters(school_year, semester)
        q = session.query(ActivityCourse).filter(
            ActivityCourse.is_active.is_(True),
            ActivityCourse.school_year == sy,
            ActivityCourse.semester == sem,
        )
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
                .join(
                    ActivityRegistration,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .filter(
                    RegistrationCourse.course_id.in_(course_ids),
                    ActivityRegistration.is_active.is_(True),
                    RegistrationCourse.status.in_(
                        ["enrolled", "waitlist", "promoted_pending"]
                    ),
                )
                .group_by(RegistrationCourse.course_id, RegistrationCourse.status)
                .all()
            )
            promoted_pending_map: dict = {}
            for course_id, status, cnt in count_rows:
                if status == "enrolled":
                    enrolled_map[course_id] = cnt
                elif status == "waitlist":
                    waitlist_map[course_id] = cnt
                else:  # promoted_pending
                    promoted_pending_map[course_id] = cnt

        items = []
        for c in courses:
            enrolled = enrolled_map.get(c.id, 0)
            waitlist = waitlist_map.get(c.id, 0)
            promoted_pending = promoted_pending_map.get(c.id, 0)
            capacity = c.capacity if c.capacity is not None else 30
            # remaining 以佔容量（enrolled + promoted_pending）為準
            occupying = enrolled + promoted_pending
            items.append(
                {
                    "id": c.id,
                    "name": c.name,
                    "price": c.price,
                    "sessions": c.sessions,
                    "capacity": capacity,
                    "video_url": c.video_url or "",
                    "allow_waitlist": c.allow_waitlist,
                    "description": c.description or "",
                    "school_year": c.school_year,
                    "semester": c.semester,
                    "enrolled": enrolled,
                    "promoted_pending": promoted_pending,
                    "waitlist_count": waitlist,
                    "remaining": max(0, capacity - occupying),
                }
            )
        return {
            "courses": items,
            "total": total,
            "skip": skip,
            "limit": limit,
            "school_year": sy,
            "semester": sem,
        }
    finally:
        session.close()


@router.get("/courses/{course_id}")
async def get_course_detail(
    course_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得課程詳情"""
    session = get_session()
    try:
        c = (
            session.query(ActivityCourse)
            .filter(
                ActivityCourse.id == course_id,
                ActivityCourse.is_active.is_(True),
            )
            .first()
        )
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
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """新增課程"""
    session = get_session()
    try:
        sy, sem = resolve_academic_term_filters(body.school_year, body.semester)
        existing = (
            session.query(ActivityCourse)
            .filter(
                ActivityCourse.name == body.name,
                ActivityCourse.school_year == sy,
                ActivityCourse.semester == sem,
                ActivityCourse.is_active.is_(True),
            )
            .first()
        )
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
            school_year=sy,
            semester=sem,
        )
        session.add(course)
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        return {
            "message": "課程新增成功",
            "id": course.id,
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


@router.post("/courses/copy-from-previous", status_code=201)
async def copy_courses_from_previous(
    body: CopyCoursesRequest,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """一鍵複製某學期的所有課程到另一學期（已存在同名課程跳過）。"""
    if (
        body.source_school_year == body.target_school_year
        and body.source_semester == body.target_semester
    ):
        raise HTTPException(status_code=400, detail="來源與目標學期不能相同")

    session = get_session()
    try:
        source_courses = (
            session.query(ActivityCourse)
            .filter(
                ActivityCourse.school_year == body.source_school_year,
                ActivityCourse.semester == body.source_semester,
                ActivityCourse.is_active.is_(True),
            )
            .all()
        )
        if not source_courses:
            return {
                "message": "來源學期無課程",
                "created": 0,
                "skipped": 0,
                "created_ids": [],
            }

        existing_names = {
            r[0]
            for r in session.query(ActivityCourse.name)
            .filter(
                ActivityCourse.school_year == body.target_school_year,
                ActivityCourse.semester == body.target_semester,
                ActivityCourse.is_active.is_(True),
            )
            .all()
        }

        created_ids: list = []
        skipped = 0
        for src in source_courses:
            if src.name in existing_names:
                skipped += 1
                continue
            new_course = ActivityCourse(
                name=src.name,
                price=src.price,
                sessions=src.sessions,
                capacity=src.capacity,
                video_url=src.video_url,
                allow_waitlist=src.allow_waitlist,
                description=src.description,
                school_year=body.target_school_year,
                semester=body.target_semester,
                is_active=True,
            )
            session.add(new_course)
            session.flush()
            created_ids.append(new_course.id)

        session.commit()
        _invalidate_activity_dashboard_caches(session)
        logger.warning(
            "複製上學期課程：from %s-%s to %s-%s created=%d skipped=%d operator=%s",
            body.source_school_year,
            body.source_semester,
            body.target_school_year,
            body.target_semester,
            len(created_ids),
            skipped,
            current_user.get("username", ""),
        )
        return {
            "message": f"複製完成：新增 {len(created_ids)} 筆、跳過 {skipped} 筆（已存在）",
            "created": len(created_ids),
            "skipped": skipped,
            "created_ids": created_ids,
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.put("/courses/{course_id}")
async def update_course(
    course_id: int,
    body: CourseUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """更新課程"""
    session = get_session()
    try:
        course = (
            session.query(ActivityCourse)
            .filter(
                ActivityCourse.id == course_id,
                ActivityCourse.is_active.is_(True),
            )
            .first()
        )
        if not course:
            raise _not_found("課程")

        if body.name and body.name != course.name:
            dup = (
                session.query(ActivityCourse)
                .filter(
                    ActivityCourse.name == body.name,
                    ActivityCourse.id != course_id,
                )
                .first()
            )
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
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/courses/{course_id}/waitlist")
async def get_course_waitlist(
    course_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得課程候補名單（按報名序排列）"""
    session = get_session()
    try:
        course = (
            session.query(ActivityCourse)
            .filter(
                ActivityCourse.id == course_id,
                ActivityCourse.is_active.is_(True),
            )
            .first()
        )
        if not course:
            raise _not_found("課程")
        rows = (
            session.query(
                RegistrationCourse.id.label("course_record_id"),
                RegistrationCourse.registration_id,
                ActivityRegistration.student_name,
                ActivityRegistration.class_name,
            )
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
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
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """停用課程（有報名者回傳 409）"""
    session = get_session()
    try:
        course = (
            session.query(ActivityCourse)
            .filter(
                ActivityCourse.id == course_id,
                ActivityCourse.is_active.is_(True),
            )
            .first()
        )
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
        raise_safe_500(e)
    finally:
        session.close()
