"""
api/activity/public.py — 公開前台端點（無需認證，10 個）
"""

import logging
from datetime import datetime
from pathlib import Path

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, Response as PlainResponse
from sqlalchemy import func

from utils.storage import get_storage_path

_POSTER_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_POSTER_MODULE = "activity_posters"


def _poster_dir() -> Path:
    return get_storage_path(_POSTER_MODULE)


_POSTER_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

from models.database import (
    get_session,
    Classroom,
    ActivityCourse,
    ActivitySupply,
    ActivityRegistration,
    RegistrationCourse,
    RegistrationSupply,
    ParentInquiry,
    ActivityRegistrationSettings,
)
from services.activity_service import activity_service
from utils.rate_limit import SlidingWindowLimiter

from ._shared import (
    PublicCourseItem,
    PublicSupplyItem,
    PublicRegistrationPayload,
    PublicUpdatePayload,
    PublicInquiryPayload,
    _not_found,
    _item_not_found_in_list,
    _invalid_class,
    _get_active_classroom,
    _invalidate_activity_dashboard_caches,
    _derive_payment_status,
    _check_registration_open,
    _attach_courses,
    _attach_supplies,
    _match_student_with_parent_phone,
    _normalize_phone,
    TAIPEI_TZ,
)
from utils.academic import resolve_academic_term_filters

logger = logging.getLogger(__name__)
router = APIRouter()

_public_query_limiter_instance = SlidingWindowLimiter(
    max_calls=10,
    window_seconds=60,
    name="activity_public_query",
    error_detail="查詢過於頻繁，請稍後再試",
)
_public_query_limiter = _public_query_limiter_instance.as_dependency()

_public_register_limiter_instance = SlidingWindowLimiter(
    max_calls=5,
    window_seconds=60,
    name="activity_public_register",
    error_detail="提交過於頻繁，請稍後再試",
)
_public_register_limiter = _public_register_limiter_instance.as_dependency()

# 家長提問：相較報名放寬一些，避免誤擋連續補充問題
_public_inquiry_limiter_instance = SlidingWindowLimiter(
    max_calls=3,
    window_seconds=60,
    name="activity_public_inquiry",
    error_detail="提交過於頻繁，請稍後再試",
)
_public_inquiry_limiter = _public_inquiry_limiter_instance.as_dependency()


_PUBLIC_DISPLAY_FIELDS = (
    "page_title",
    "term_label",
    "event_date_label",
    "target_audience",
    "form_card_title",
    "poster_url",
)


@router.get("/public/registration-time")
async def get_public_registration_time(response: Response):
    """公開端點：前台查詢報名開放時間 + 顯示設定（無需認證）"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        response.headers["Cache-Control"] = "public, max-age=60"
        if not settings:
            return {
                "is_open": False,
                "open_at": None,
                "close_at": None,
                **{k: None for k in _PUBLIC_DISPLAY_FIELDS},
            }
        return {
            "is_open": settings.is_open,
            "open_at": settings.open_at,
            "close_at": settings.close_at,
            **{k: getattr(settings, k, None) for k in _PUBLIC_DISPLAY_FIELDS},
        }
    finally:
        session.close()


@router.get("/public/poster/{filename}")
async def get_public_poster(filename: str, response: Response):
    """公開端點：回傳已上傳的活動海報圖。

    防穿越：檔名只允許純 hex + 白名單副檔名，同時驗證檔案位於 _POSTER_DIR。
    """
    path = Path(filename)
    if path.name != filename or ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="非法檔名")
    ext = path.suffix.lower()
    stem = path.stem
    if (
        ext not in _POSTER_ALLOWED_EXT
        or not stem
        or not all(c in "0123456789abcdef" for c in stem)
    ):
        raise HTTPException(status_code=400, detail="非法檔名")

    poster_dir = _poster_dir()
    full_path = (poster_dir / filename).resolve()
    try:
        full_path.relative_to(poster_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="非法路徑")
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="海報不存在")

    response.headers["Cache-Control"] = "public, max-age=300"
    return FileResponse(str(full_path), media_type=_POSTER_MIME.get(ext, "image/*"))


@router.get("/public/courses")
async def get_public_courses(response: Response):
    """前台：取得課程列表"""
    session = get_session()
    try:
        courses = (
            session.query(ActivityCourse)
            .filter(ActivityCourse.is_active.is_(True))
            .order_by(ActivityCourse.id)
            .all()
        )
        response.headers["Cache-Control"] = (
            "public, max-age=300, stale-while-revalidate=60"
        )
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
        supplies = (
            session.query(ActivitySupply)
            .filter(ActivitySupply.is_active.is_(True))
            .order_by(ActivitySupply.id)
            .all()
        )
        response.headers["Cache-Control"] = (
            "public, max-age=300, stale-while-revalidate=60"
        )
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
        courses = (
            session.query(ActivityCourse)
            .filter(ActivityCourse.is_active.is_(True))
            .all()
        )
        course_ids = [c.id for c in courses]
        enrolled_map = (
            dict(
                session.query(
                    RegistrationCourse.course_id, func.count(RegistrationCourse.id)
                )
                .join(
                    ActivityRegistration,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .filter(
                    RegistrationCourse.course_id.in_(course_ids),
                    RegistrationCourse.status == "enrolled",
                    ActivityRegistration.is_active.is_(True),
                )
                .group_by(RegistrationCourse.course_id)
                .all()
            )
            if course_ids
            else {}
        )
        availability = {}
        for course in courses:
            enrolled = enrolled_map.get(course.id, 0)
            capacity = course.capacity if course.capacity is not None else 30
            remaining = capacity - enrolled
            if remaining <= 0:
                availability[course.name] = -1 if not course.allow_waitlist else 0
            else:
                availability[course.name] = remaining
        etag = (
            '"'
            + hashlib.md5(json.dumps(availability, sort_keys=True).encode()).hexdigest()
            + '"'
        )
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
        courses = (
            session.query(ActivityCourse)
            .filter(
                ActivityCourse.is_active.is_(True),
                ActivityCourse.video_url.isnot(None),
                ActivityCourse.video_url != "",
            )
            .all()
        )
        response.headers["Cache-Control"] = "public, max-age=300"
        return {c.name: c.video_url for c in courses}
    finally:
        session.close()


@router.get("/public/query")
async def public_query_registration(
    name: str,
    birthday: str,
    parent_phone: str,
    _: None = Depends(_public_query_limiter),
):
    """前台：依姓名+生日+家長手機查詢報名資料

    三欄必須同時相符；任一欄不符一律回相同的通用錯誤（不洩漏是哪一欄不符）。
    """
    session = get_session()
    try:
        normalized_phone = _normalize_phone(parent_phone)
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.student_name == name,
                ActivityRegistration.birthday == birthday,
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        # 通用錯誤：找不到 / phone 不符 → 同一個錯誤，避免透露哪一欄錯
        if not reg or _normalize_phone(reg.parent_phone) != normalized_phone:
            raise HTTPException(
                status_code=404,
                detail="查無對應報名，請確認三項資料是否與報名時一致",
            )

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
                    func.row_number()
                    .over(
                        partition_by=RegistrationCourse.course_id,
                        order_by=RegistrationCourse.id,
                    )
                    .label("position"),
                )
                .join(
                    ActivityRegistration,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .filter(
                    RegistrationCourse.course_id.in_(waitlist_course_ids),
                    RegistrationCourse.status == "waitlist",
                    ActivityRegistration.is_active.is_(True),
                )
                .subquery()
            )
            waitlist_rows = (
                session.query(stmt).filter(stmt.c.registration_id == reg.id).all()
            )
            waitlist_position_map = {
                row.course_id: row.position for row in waitlist_rows
            }

        courses = []
        for rc, ac in rc_rows:
            waitlist_position = None
            if rc.status == "waitlist":
                waitlist_position = waitlist_position_map.get(ac.id)
            courses.append(
                {
                    "name": ac.name,
                    "price": rc.price_snapshot,
                    "status": rc.status,
                    "waitlist_position": waitlist_position,
                }
            )

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
    """前台：提交報名表（分學期、靜默比對在校生、失敗進入待審核佇列）。

    隱私契約：response 絕不洩漏 match_status / classroom_id / student_id /
    pending_review 等任何比對結果；成功/失敗家長看到同樣的中性訊息。
    """
    session = get_session()
    try:
        _check_registration_open(session)

        # 決定學期（未傳則用當前）
        sy, sem = resolve_academic_term_filters(body.school_year, body.semester)

        # 重複報名防護（同學期內同學生不可重複）
        existing = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.student_name == body.name,
                ActivityRegistration.birthday == body.birthday,
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.school_year == sy,
                ActivityRegistration.semester == sem,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=400, detail="此學生本學期已有有效報名，請使用修改功能"
            )

        # Soft dedup：同 parent_phone + 學期若已有 pending 筆，擋重複送件
        # （避免家長錯字重送產生一堆 pending）
        pending_dup = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.parent_phone == body.parent_phone,
                ActivityRegistration.pending_review.is_(True),
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.school_year == sy,
                ActivityRegistration.semester == sem,
            )
            .first()
        )
        if pending_dup:
            raise HTTPException(
                status_code=400,
                detail="您的報名仍在確認中，如需補件請直接聯繫校方",
            )

        # 三欄靜默比對：name + birthday + parent_phone
        matched_student_id, matched_classroom_id = _match_student_with_parent_phone(
            session, body.name, body.birthday, body.parent_phone
        )

        # 班級來源：匹配成功以 Student.classroom 為準（覆蓋家長自選），
        # 失敗則保留家長輸入字串作為審核參考。
        classroom_name_to_store = body.class_
        if matched_student_id and matched_classroom_id:
            real_classroom = (
                session.query(Classroom)
                .filter(Classroom.id == matched_classroom_id)
                .first()
            )
            if real_classroom:
                classroom_name_to_store = real_classroom.name
            else:
                # Student.classroom_id 指向已停用/不存在班級，退回待審核
                matched_student_id = None
                matched_classroom_id = None

        course_names = [item.name for item in body.courses]
        if len(course_names) != len(set(course_names)):
            raise HTTPException(status_code=400, detail="課程清單中有重複項目")
        supply_names = [item.name for item in body.supplies]
        if len(supply_names) != len(set(supply_names)):
            raise HTTPException(status_code=400, detail="用品清單中有重複項目")

        # 課程/用品限定同學期
        courses_by_name = (
            {
                c.name: c
                for c in session.query(ActivityCourse)
                .filter(
                    ActivityCourse.name.in_(course_names),
                    ActivityCourse.is_active.is_(True),
                    ActivityCourse.school_year == sy,
                    ActivityCourse.semester == sem,
                )
                .with_for_update()
                .all()
            }
            if course_names
            else {}
        )

        supplies_by_name = (
            {
                s.name: s
                for s in session.query(ActivitySupply)
                .filter(
                    ActivitySupply.name.in_(supply_names),
                    ActivitySupply.is_active.is_(True),
                    ActivitySupply.school_year == sy,
                    ActivitySupply.semester == sem,
                )
                .all()
            }
            if supply_names
            else {}
        )

        _reg_course_ids = [c.id for c in courses_by_name.values()]
        enrolled_count_map = (
            dict(
                session.query(
                    RegistrationCourse.course_id, func.count(RegistrationCourse.id)
                )
                .join(
                    ActivityRegistration,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .filter(
                    RegistrationCourse.course_id.in_(_reg_course_ids),
                    RegistrationCourse.status == "enrolled",
                    ActivityRegistration.is_active.is_(True),
                )
                .group_by(RegistrationCourse.course_id)
                .all()
            )
            if _reg_course_ids
            else {}
        )

        is_matched = bool(matched_student_id and matched_classroom_id)

        reg = ActivityRegistration(
            student_name=body.name,
            birthday=body.birthday,
            class_name=classroom_name_to_store,
            school_year=sy,
            semester=sem,
            student_id=matched_student_id,
            classroom_id=matched_classroom_id,
            parent_phone=body.parent_phone,
            remark=(body.remark or "").strip(),
            pending_review=not is_matched,
            match_status="matched" if is_matched else "pending",
        )
        session.add(reg)
        session.flush()

        has_waitlist, waitlist_course_names = _attach_courses(
            session, reg.id, body.courses, courses_by_name, enrolled_count_map
        )
        _attach_supplies(session, reg.id, body.supplies, supplies_by_name)

        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        logger.info(
            "新報名提交：id=%s student=%s matched=%s",
            reg.id,
            reg.student_name,
            is_matched,
        )

        # 中性回覆：成功/失敗家長看到相同訊息（不洩漏比對結果）
        # waitlist 仍揭露（那是家長自己勾的課程）
        msg = (
            "報名資料已送出，您有課程進入候補名單，校方將儘快與您聯繫。"
            if has_waitlist
            else "報名資料已送出，校方將於 1-2 個工作天確認後主動與您聯繫。"
        )
        return {
            "message": msg,
            "id": reg.id,
            "waitlisted": has_waitlist,
            "waitlist_courses": waitlist_course_names,
        }
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

        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == body.id,
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        # 通用錯誤：查不到 / 三欄不符一律回相同訊息
        normalized_phone = _normalize_phone(body.parent_phone)
        if (
            not reg
            or reg.student_name != body.name
            or reg.birthday != body.birthday
            or _normalize_phone(reg.parent_phone) != normalized_phone
        ):
            raise HTTPException(
                status_code=403,
                detail="查無對應報名，請確認三項資料是否與報名時一致",
            )

        # 匹配成功後的報名，班級由系統維護（Student.classroom），家長輸入班級僅供參考
        if reg.classroom_id:
            real_classroom = (
                session.query(Classroom)
                .filter(Classroom.id == reg.classroom_id)
                .first()
            )
            # 若班級仍存在，覆蓋為真實班級；否則 fallback 到家長輸入
            classroom_name_to_store = (
                real_classroom.name if real_classroom else body.class_
            )
        else:
            # 仍處於 pending 的報名，允許家長透過更新修正資料（會重跑比對）
            classroom_name_to_store = body.class_

        course_names = [item.name for item in body.courses]
        if len(course_names) != len(set(course_names)):
            raise HTTPException(status_code=400, detail="課程清單中有重複項目")
        supply_names = [item.name for item in body.supplies]
        if len(supply_names) != len(set(supply_names)):
            raise HTTPException(status_code=400, detail="用品清單中有重複項目")

        courses_by_name = (
            {
                c.name: c
                for c in session.query(ActivityCourse)
                .filter(
                    ActivityCourse.name.in_(course_names),
                    ActivityCourse.is_active.is_(True),
                )
                .with_for_update()
                .all()
            }
            if course_names
            else {}
        )

        supplies_by_name = (
            {
                s.name: s
                for s in session.query(ActivitySupply)
                .filter(
                    ActivitySupply.name.in_(supply_names),
                    ActivitySupply.is_active.is_(True),
                )
                .all()
            }
            if supply_names
            else {}
        )

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

        reg.class_name = classroom_name_to_store
        # pending 狀態下，家長修改 phone 後自動重跑比對，成功則解除 pending
        if reg.pending_review:
            new_sid, new_cid = _match_student_with_parent_phone(
                session, reg.student_name, reg.birthday, body.parent_phone
            )
            if new_sid and new_cid:
                real = session.query(Classroom).filter(Classroom.id == new_cid).first()
                if real:
                    reg.student_id = new_sid
                    reg.classroom_id = new_cid
                    reg.class_name = real.name
                    reg.pending_review = False
                    reg.match_status = "matched"
            reg.parent_phone = body.parent_phone

        _upd_course_ids = [c.id for c in courses_by_name.values()]
        upd_enrolled_map = (
            dict(
                session.query(
                    RegistrationCourse.course_id, func.count(RegistrationCourse.id)
                )
                .join(
                    ActivityRegistration,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .filter(
                    RegistrationCourse.course_id.in_(_upd_course_ids),
                    RegistrationCourse.status == "enrolled",
                    ActivityRegistration.is_active.is_(True),
                    ActivityRegistration.id != reg.id,
                )
                .group_by(RegistrationCourse.course_id)
                .all()
            )
            if _upd_course_ids
            else {}
        )

        _attach_courses(
            session, reg.id, body.courses, courses_by_name, upd_enrolled_map
        )
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
async def public_create_inquiry(
    body: PublicInquiryPayload,
    _: None = Depends(_public_inquiry_limiter),
):
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
