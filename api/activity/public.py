"""
api/activity/public.py — 公開前台端點（無需認證，10 個）
"""

import logging
from datetime import datetime

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import Response as PlainResponse
from sqlalchemy import func

from models.database import (
    get_session, Classroom,
    ActivityCourse, ActivitySupply, ActivityRegistration,
    RegistrationCourse, RegistrationSupply,
    ParentInquiry, ActivityRegistrationSettings,
)
from services.activity_service import activity_service
from utils.rate_limit import SlidingWindowLimiter

from ._shared import (
    PublicCourseItem, PublicSupplyItem,
    PublicRegistrationPayload, PublicUpdatePayload, PublicInquiryPayload,
    _not_found, _item_not_found_in_list, _invalid_class,
    _require_active_classroom, _invalidate_activity_dashboard_caches,
    _derive_payment_status, _check_registration_open,
    _attach_courses, _attach_supplies,
    TAIPEI_TZ,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_public_query_limiter = SlidingWindowLimiter(
    max_calls=10,
    window_seconds=60,
    name="activity_public_query",
    error_detail="查詢過於頻繁，請稍後再試",
).as_dependency()

_public_register_limiter = SlidingWindowLimiter(
    max_calls=5,
    window_seconds=60,
    name="activity_public_register",
    error_detail="提交過於頻繁，請稍後再試",
).as_dependency()


@router.get("/public/registration-time")
async def get_public_registration_time(response: Response):
    """公開端點：前台查詢報名開放時間（無需認證）"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            response.headers["Cache-Control"] = "public, max-age=60"
            return {"is_open": False, "open_at": None, "close_at": None}
        response.headers["Cache-Control"] = "public, max-age=60"
        return {
            "is_open": settings.is_open,
            "open_at": settings.open_at,
            "close_at": settings.close_at,
        }
    finally:
        session.close()


@router.get("/public/courses")
async def get_public_courses(response: Response):
    """前台：取得課程列表"""
    session = get_session()
    try:
        courses = session.query(ActivityCourse).filter(
            ActivityCourse.is_active.is_(True)
        ).order_by(ActivityCourse.id).all()
        response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=60"
        return [
            {"name": c.name, "price": c.price, "sessions": c.sessions, "frequency": ""}
            for c in courses
        ]
    finally:
        session.close()


@router.get("/public/supplies")
async def get_public_supplies(response: Response):
    """前台：取得用品列表"""
    session = get_session()
    try:
        supplies = session.query(ActivitySupply).filter(
            ActivitySupply.is_active.is_(True)
        ).order_by(ActivitySupply.id).all()
        response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=60"
        return [{"name": s.name, "price": s.price} for s in supplies]
    finally:
        session.close()


@router.get("/public/classes")
async def get_public_classes(response: Response):
    """前台：取得班級選項"""
    session = get_session()
    try:
        classrooms = (
            session.query(Classroom)
            .filter(Classroom.is_active.is_(True))
            .order_by(Classroom.id)
            .all()
        )
        response.headers["Cache-Control"] = "public, max-age=600"
        return [c.name for c in classrooms]
    finally:
        session.close()


@router.get("/public/courses/availability")
async def get_public_courses_availability(request: Request, response: Response):
    """前台：取得課程名額狀況"""
    session = get_session()
    try:
        courses = session.query(ActivityCourse).filter(
            ActivityCourse.is_active.is_(True)
        ).all()
        course_ids = [c.id for c in courses]
        enrolled_map = dict(
            session.query(RegistrationCourse.course_id, func.count(RegistrationCourse.id))
            .join(ActivityRegistration, RegistrationCourse.registration_id == ActivityRegistration.id)
            .filter(
                RegistrationCourse.course_id.in_(course_ids),
                RegistrationCourse.status == "enrolled",
                ActivityRegistration.is_active.is_(True),
            )
            .group_by(RegistrationCourse.course_id)
            .all()
        ) if course_ids else {}
        availability = {}
        for course in courses:
            enrolled = enrolled_map.get(course.id, 0)
            capacity = course.capacity if course.capacity is not None else 30
            remaining = capacity - enrolled
            if remaining <= 0:
                availability[course.name] = -1 if not course.allow_waitlist else 0
            else:
                availability[course.name] = remaining
        etag = '"' + hashlib.md5(json.dumps(availability, sort_keys=True).encode()).hexdigest() + '"'
        response.headers["ETag"] = etag
        response.headers["Cache-Control"] = "no-cache"
        if request.headers.get("If-None-Match") == etag:
            return PlainResponse(status_code=304)
        return availability
    finally:
        session.close()


@router.get("/public/course-videos")
async def get_public_course_videos(response: Response):
    """前台：取得課程介紹影片 URL"""
    session = get_session()
    try:
        courses = session.query(ActivityCourse).filter(
            ActivityCourse.is_active.is_(True),
            ActivityCourse.video_url.isnot(None),
            ActivityCourse.video_url != "",
        ).all()
        response.headers["Cache-Control"] = "public, max-age=300"
        return {c.name: c.video_url for c in courses}
    finally:
        session.close()


@router.get("/public/query")
async def public_query_registration(
    name: str,
    birthday: str,
    _: None = Depends(_public_query_limiter),
):
    """前台：依姓名+生日查詢報名資料"""
    session = get_session()
    try:
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.student_name == name,
            ActivityRegistration.birthday == birthday,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise _not_found("該名幼兒的報名資料")

        rc_rows = (
            session.query(RegistrationCourse, ActivityCourse)
            .join(ActivityCourse, RegistrationCourse.course_id == ActivityCourse.id)
            .filter(RegistrationCourse.registration_id == reg.id)
            .all()
        )

        # 一次查出所有候補課程的排位（window function，避免 N+1）
        waitlist_course_ids = [ac.id for rc, ac in rc_rows if rc.status == "waitlist"]
        waitlist_position_map: dict[int, int] = {}
        if waitlist_course_ids:
            stmt = (
                session.query(
                    RegistrationCourse.registration_id,
                    RegistrationCourse.course_id,
                    func.row_number().over(
                        partition_by=RegistrationCourse.course_id,
                        order_by=RegistrationCourse.id,
                    ).label("position"),
                )
                .join(ActivityRegistration, RegistrationCourse.registration_id == ActivityRegistration.id)
                .filter(
                    RegistrationCourse.course_id.in_(waitlist_course_ids),
                    RegistrationCourse.status == "waitlist",
                    ActivityRegistration.is_active.is_(True),
                )
                .subquery()
            )
            waitlist_rows = session.query(stmt).filter(
                stmt.c.registration_id == reg.id
            ).all()
            waitlist_position_map = {row.course_id: row.position for row in waitlist_rows}

        courses = []
        for rc, ac in rc_rows:
            waitlist_position = None
            if rc.status == "waitlist":
                waitlist_position = waitlist_position_map.get(ac.id)
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

        total_amount = sum(c["price"] for c in courses if c["status"] == "enrolled")
        total_amount += sum(rs.price_snapshot for rs, sp in rs_rows)
        paid_amount = reg.paid_amount or 0

        return {
            "id": reg.id,
            "name": reg.student_name,
            "birthday": reg.birthday,
            "class_name": reg.class_name,
            "is_paid": reg.is_paid,
            "paid_amount": paid_amount,
            "total_amount": total_amount,
            "payment_status": _derive_payment_status(paid_amount, total_amount),
            "remark": reg.remark or "",
            "courses": courses,
            "supplies": [sp.name for rs, sp in rs_rows],
        }
    finally:
        session.close()


@router.post("/public/register", status_code=201)
async def public_register(
    body: PublicRegistrationPayload,
    _: None = Depends(_public_register_limiter),
):
    """前台：提交報名表"""
    session = get_session()
    try:
        _check_registration_open(session)

        # 重複報名防護
        existing = session.query(ActivityRegistration).filter(
            ActivityRegistration.student_name == body.name,
            ActivityRegistration.birthday == body.birthday,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="此學生已有有效報名，請使用修改功能")

        classroom = _require_active_classroom(session, body.class_)

        course_names = [item.name for item in body.courses]
        if len(course_names) != len(set(course_names)):
            raise HTTPException(status_code=400, detail="課程清單中有重複項目")
        supply_names = [item.name for item in body.supplies]
        if len(supply_names) != len(set(supply_names)):
            raise HTTPException(status_code=400, detail="用品清單中有重複項目")

        courses_by_name = {c.name: c for c in session.query(ActivityCourse).filter(
            ActivityCourse.name.in_(course_names),
            ActivityCourse.is_active.is_(True),
        ).with_for_update().all()} if course_names else {}

        supplies_by_name = {s.name: s for s in session.query(ActivitySupply).filter(
            ActivitySupply.name.in_(supply_names),
            ActivitySupply.is_active.is_(True),
        ).all()} if supply_names else {}

        _reg_course_ids = [c.id for c in courses_by_name.values()]
        enrolled_count_map = dict(
            session.query(RegistrationCourse.course_id, func.count(RegistrationCourse.id))
            .join(ActivityRegistration, RegistrationCourse.registration_id == ActivityRegistration.id)
            .filter(
                RegistrationCourse.course_id.in_(_reg_course_ids),
                RegistrationCourse.status == "enrolled",
                ActivityRegistration.is_active.is_(True),
            )
            .group_by(RegistrationCourse.course_id)
            .all()
        ) if _reg_course_ids else {}

        reg = ActivityRegistration(
            student_name=body.name,
            birthday=body.birthday,
            class_name=classroom.name,
        )
        session.add(reg)
        session.flush()

        has_waitlist, waitlist_course_names = _attach_courses(
            session, reg.id, body.courses, courses_by_name, enrolled_count_map
        )
        _attach_supplies(session, reg.id, body.supplies, supplies_by_name)

        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        logger.info("新報名提交：id=%s student=%s", reg.id, reg.student_name)

        msg = (
            "報名成功！您有課程進入候補名單，我們會儘快通知您。"
            if has_waitlist
            else "報名成功！感謝您的報名。"
        )
        return {"message": msg, "id": reg.id, "waitlisted": has_waitlist, "waitlist_courses": waitlist_course_names}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logger.error("公開報名失敗：%s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.post("/public/update")
async def public_update_registration(
    body: PublicUpdatePayload,
    _: None = Depends(_public_register_limiter),
):
    """前台：依 id 更新報名資料（班級/課程/用品）"""
    session = get_session()
    try:
        _check_registration_open(session)

        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == body.id,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise _not_found("報名資料")

        if reg.student_name != body.name or reg.birthday != body.birthday:
            raise HTTPException(status_code=403, detail="姓名或生日不符，無法修改")

        classroom = _require_active_classroom(session, body.class_)

        course_names = [item.name for item in body.courses]
        if len(course_names) != len(set(course_names)):
            raise HTTPException(status_code=400, detail="課程清單中有重複項目")
        supply_names = [item.name for item in body.supplies]
        if len(supply_names) != len(set(supply_names)):
            raise HTTPException(status_code=400, detail="用品清單中有重複項目")

        courses_by_name = {c.name: c for c in session.query(ActivityCourse).filter(
            ActivityCourse.name.in_(course_names),
            ActivityCourse.is_active.is_(True),
        ).with_for_update().all()} if course_names else {}

        supplies_by_name = {s.name: s for s in session.query(ActivitySupply).filter(
            ActivitySupply.name.in_(supply_names),
            ActivitySupply.is_active.is_(True),
        ).all()} if supply_names else {}

        for course_item in body.courses:
            if course_item.name not in courses_by_name:
                raise _item_not_found_in_list("課程", course_item.name)
        for supply_item in body.supplies:
            if supply_item.name not in supplies_by_name:
                raise _item_not_found_in_list("用品", supply_item.name)

        session.query(RegistrationCourse).filter(
            RegistrationCourse.registration_id == reg.id
        ).delete()
        session.query(RegistrationSupply).filter(
            RegistrationSupply.registration_id == reg.id
        ).delete()
        session.flush()

        reg.class_name = classroom.name

        _upd_course_ids = [c.id for c in courses_by_name.values()]
        upd_enrolled_map = dict(
            session.query(RegistrationCourse.course_id, func.count(RegistrationCourse.id))
            .join(ActivityRegistration, RegistrationCourse.registration_id == ActivityRegistration.id)
            .filter(
                RegistrationCourse.course_id.in_(_upd_course_ids),
                RegistrationCourse.status == "enrolled",
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.id != reg.id,
            )
            .group_by(RegistrationCourse.course_id)
            .all()
        ) if _upd_course_ids else {}

        _attach_courses(session, reg.id, body.courses, courses_by_name, upd_enrolled_map)
        _attach_supplies(session, reg.id, body.supplies, supplies_by_name)

        reg.remark = body.remark
        session.commit()
        _invalidate_activity_dashboard_caches(session)
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
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        return {"message": "感謝您的提問，我們會儘快回覆您！"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
