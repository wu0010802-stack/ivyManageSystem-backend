"""
api/activity/public.py — 公開前台端點（無需認證，10 個）
"""

import asyncio
import logging
import random
from datetime import datetime
from pathlib import Path

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, Response as PlainResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from utils.errors import raise_safe_500
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
    ActivitySession,
    ActivityAttendance,
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
    should_silent_reject_bot,
    _not_found,
    _item_not_found_in_list,
    _invalid_class,
    _get_active_classroom,
    _invalidate_activity_dashboard_caches,
    _derive_payment_status,
    _check_registration_open,
    _attach_courses,
    _attach_supplies,
    _calc_total_amount,
    _compute_is_paid,
    _match_student_with_parent_phone,
    _normalize_phone,
    _public_etag_response,
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
async def get_public_registration_time(request: Request, response: Response):
    """公開端點：前台查詢報名開放時間 + 顯示設定（無需認證）"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            payload = {
                "is_open": False,
                "open_at": None,
                "close_at": None,
                **{k: None for k in _PUBLIC_DISPLAY_FIELDS},
            }
        else:
            payload = {
                "is_open": settings.is_open,
                "open_at": settings.open_at,
                "close_at": settings.close_at,
                **{k: getattr(settings, k, None) for k in _PUBLIC_DISPLAY_FIELDS},
            }
        return _public_etag_response(request, response, payload)
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
async def get_public_courses(request: Request, response: Response):
    """前台：取得課程列表"""
    session = get_session()
    try:
        courses = (
            session.query(ActivityCourse)
            .filter(ActivityCourse.is_active.is_(True))
            .order_by(ActivityCourse.id)
            .all()
        )
        payload = [
            {"name": c.name, "price": c.price, "sessions": c.sessions, "frequency": ""}
            for c in courses
        ]
        return _public_etag_response(request, response, payload)
    finally:
        session.close()


@router.get("/public/supplies")
async def get_public_supplies(request: Request, response: Response):
    """前台：取得用品列表"""
    session = get_session()
    try:
        supplies = (
            session.query(ActivitySupply)
            .filter(ActivitySupply.is_active.is_(True))
            .order_by(ActivitySupply.id)
            .all()
        )
        payload = [{"name": s.name, "price": s.price} for s in supplies]
        return _public_etag_response(request, response, payload)
    finally:
        session.close()


@router.get("/public/classes")
async def get_public_classes(request: Request, response: Response):
    """前台：取得班級選項"""
    session = get_session()
    try:
        classrooms = (
            session.query(Classroom)
            .filter(Classroom.is_active.is_(True))
            .order_by(Classroom.id)
            .all()
        )
        payload = [c.name for c in classrooms]
        return _public_etag_response(request, response, payload)
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
        # 佔容量 = enrolled + promoted_pending（兩者皆已佔名額，避免超發候補通知）
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
                    RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
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
async def get_public_course_videos(request: Request, response: Response):
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
        payload = {c.name: c.video_url for c in courses}
        return _public_etag_response(request, response, payload)
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

    LOW-3：對成功與失敗 path 加入 200~500ms 隨機延遲，提高低成本枚舉成本。
    """
    await asyncio.sleep(random.uniform(0.2, 0.5))
    session = get_session()
    try:
        normalized_phone = _normalize_phone(parent_phone)
        # 先抓 (name, birthday) 候選（同姓同生日通常極少），再統一在 Python 端
        # 比對 normalize 後的 phone；無論是否匹配都走相同程式路徑，壓低時序差。
        candidates = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.student_name == name,
                ActivityRegistration.birthday == birthday,
                ActivityRegistration.is_active.is_(True),
            )
            .all()
        )
        reg = None
        for candidate in candidates:
            if _normalize_phone(candidate.parent_phone) == normalized_phone:
                reg = candidate
                break
        if reg is None:
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
                    "course_id": ac.id,
                    "price": rc.price_snapshot,
                    "status": rc.status,
                    "waitlist_position": waitlist_position,
                    # 候補升正式待確認資訊（僅 promoted_pending 有效）
                    "confirm_deadline": (
                        rc.confirm_deadline.isoformat()
                        if rc.status == "promoted_pending" and rc.confirm_deadline
                        else None
                    ),
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

    LOW-4：honeypot + 時序檢查若命中 → silent reject（回偽裝成功訊息、不寫 DB）。
    """
    if should_silent_reject_bot(body.hp, body.ts):
        logger.warning(
            "public_register silent-reject (honeypot/ts) name=%r phone=%r",
            body.name,
            body.parent_phone,
        )
        return {
            "message": "報名資料已送出，校方將於 1-2 個工作天確認後主動與您聯繫。",
            "id": 0,
            "waitlisted": False,
            "waitlist_courses": [],
        }
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
                .filter(
                    Classroom.id == matched_classroom_id,
                    Classroom.is_active.is_(True),
                )
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
        # 佔容量計算：enrolled + promoted_pending 皆算，避免對已滿的課程誤發 enrolled
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
                    RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
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

        try:
            session.commit()
        except IntegrityError as ie:
            # partial unique index `uq_activity_regs_student_term_active` 攔到並發雙寫：
            # 應用層 `existing` SELECT 與 INSERT 之間若有第二個請求穿插，DB 層才能擋下。
            session.rollback()
            msg_lower = str(getattr(ie, "orig", ie)).lower()
            if "uq_activity_regs_student_term_active" in msg_lower:
                raise HTTPException(
                    status_code=400,
                    detail="此學生本學期已有有效報名，請使用修改功能",
                )
            raise
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
        raise_safe_500(e)
    finally:
        session.close()


@router.post("/public/update")
async def public_update_registration(
    body: PublicUpdatePayload,
    _: None = Depends(_public_register_limiter),
):
    """前台：依 id 更新報名資料（班級/課程/用品）

    帳務對帳守則：
    - 若更新後會產生超繳（paid_amount > new_total）→ 一律 409 拒絕，不寫任何
      退費紀錄、不扣 paid_amount。Why: 公開端點無法執行金流簽核
      （ACTIVITY_PAYMENT_APPROVE），允許家長端自動沖帳會繞過所有金流守衛
      （無金額閘門、無原因記錄、無 admin 即時通知）。退費一律改由管理員後台
      （/registrations/{id}/payment、withdraw_course）處理。
    - 若家長把已被點名的課程移除 → 同步清該 reg 在那些課程的 ActivityAttendance
      （與 withdraw_course 一致），避免出席統計納入退課孤兒。
    - 同步 is_paid 旗標（與後台共用 _compute_is_paid，total=0 時一律未結清）。
    """
    session = get_session()
    try:
        _check_registration_open(session)

        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == body.id,
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update()
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
                .filter(
                    Classroom.id == reg.classroom_id,
                    Classroom.is_active.is_(True),
                )
                .first()
            )
            # 若班級仍存在且啟用，覆蓋為真實班級；否則 fallback 到家長輸入
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

        # 限定本筆報名所屬學期，避免上下學期同名課程/用品被誤選
        courses_by_name = (
            {
                c.name: c
                for c in session.query(ActivityCourse)
                .filter(
                    ActivityCourse.name.in_(course_names),
                    ActivityCourse.is_active.is_(True),
                    ActivityCourse.school_year == reg.school_year,
                    ActivityCourse.semester == reg.semester,
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
                    ActivitySupply.school_year == reg.school_year,
                    ActivitySupply.semester == reg.semester,
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

        # 刪除前快照原本佔容量的 course_id，稍後用來判斷是否需觸發候補遞補
        old_occupying_course_ids = {
            cid
            for (cid,) in session.query(RegistrationCourse.course_id)
            .filter(
                RegistrationCourse.registration_id == reg.id,
                RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
            )
            .all()
        }

        session.query(RegistrationCourse).filter(
            RegistrationCourse.registration_id == reg.id
        ).delete()
        session.query(RegistrationSupply).filter(
            RegistrationSupply.registration_id == reg.id
        ).delete()
        session.flush()

        reg.class_name = classroom_name_to_store

        # 處理家長換手機：body.parent_phone 是舊號（用於驗證），
        # body.new_parent_phone 若填且不同於舊號，表示家長要求變更聯絡電話。
        effective_phone = body.parent_phone
        if body.new_parent_phone and body.new_parent_phone != body.parent_phone:
            # 擋住改成「其他家長」正在使用的手機號：否則會讓三欄查詢 /public/query 候選
            # 變多，甚至讓不同家長的報名互相可見（name 同姓時）。
            # 擴大為全域 is_active（不限同學期）——否則跨學期共用同支電話會讓對帳
            # 混亂，無法還原哪支手機真正對應哪位家長。
            conflict = (
                session.query(ActivityRegistration.id)
                .filter(
                    ActivityRegistration.id != reg.id,
                    ActivityRegistration.parent_phone == body.new_parent_phone,
                    ActivityRegistration.is_active.is_(True),
                )
                .first()
            )
            if conflict is not None:
                raise HTTPException(
                    status_code=409,
                    detail="此手機號碼已被其他報名使用，請聯繫校方協助處理",
                )
            reg.parent_phone = body.new_parent_phone
            effective_phone = body.new_parent_phone

        # pending 狀態下，以（可能更新後的）電話重跑比對，成功則解除 pending
        if reg.pending_review:
            new_sid, new_cid = _match_student_with_parent_phone(
                session, reg.student_name, reg.birthday, effective_phone
            )
            if new_sid and new_cid:
                real = (
                    session.query(Classroom)
                    .filter(
                        Classroom.id == new_cid,
                        Classroom.is_active.is_(True),
                    )
                    .first()
                )
                if real:
                    reg.student_id = new_sid
                    reg.classroom_id = new_cid
                    reg.class_name = real.name
                    reg.pending_review = False
                    reg.match_status = "matched"

        _upd_course_ids = [c.id for c in courses_by_name.values()]
        # 佔容量 = enrolled + promoted_pending（排除當前這筆 reg）
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
                    RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
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

        # 對於原本佔容量、這次修改後此 reg 已不再占的課程，逐一觸發候補遞補
        session.flush()
        new_occupying_course_ids = {
            cid
            for (cid,) in session.query(RegistrationCourse.course_id)
            .filter(
                RegistrationCourse.registration_id == reg.id,
                RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
            )
            .all()
        }
        vacated_course_ids = old_occupying_course_ids - new_occupying_course_ids
        for vacated_cid in vacated_course_ids:
            activity_service._auto_promote_first_waitlist(session, vacated_cid)

        # 清除已不再報名課程的 ActivityAttendance 孤兒紀錄（與管理端 withdraw_course
        # 對齊）。否則退課學生仍掛在 attendance，污染出席率統計與點名表。
        if vacated_course_ids:
            session_ids_subq = (
                session.query(ActivitySession.id)
                .filter(ActivitySession.course_id.in_(vacated_course_ids))
                .subquery()
            )
            session.query(ActivityAttendance).filter(
                ActivityAttendance.registration_id == reg.id,
                ActivityAttendance.session_id.in_(session_ids_subq),
            ).delete(synchronize_session=False)

        reg.remark = body.remark

        # 超繳一律拒絕（不再自動沖帳）。理由詳見 docstring；replace 後若需退費，
        # 請家長改聯繫校方由管理員執行帶簽核權限的退費流程。
        paid_amount = reg.paid_amount or 0
        new_total = _calc_total_amount(session, reg.id)
        if paid_amount > new_total:
            refund_needed = paid_amount - new_total
            raise HTTPException(
                status_code=409,
                detail=(
                    f"此次更新會產生退費 NT${refund_needed}，"
                    "為確保金流安全無法於前台直接處理。"
                    "請改聯繫校方協助更新資料。"
                ),
            )
        reg.is_paid = _compute_is_paid(reg.paid_amount or 0, new_total)

        session.commit()
        _invalidate_activity_dashboard_caches(session)
        logger.info("前台更新報名：id=%s student=%s", reg.id, reg.student_name)
        return {
            "message": "資料更新成功！",
            "total_amount": new_total,
            "paid_amount": reg.paid_amount or 0,
            "payment_status": _derive_payment_status(reg.paid_amount or 0, new_total),
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logger.error("前台更新報名失敗：%s", e)
        raise_safe_500(e)
    finally:
        session.close()


_public_confirm_limiter_instance = SlidingWindowLimiter(
    max_calls=10,
    window_seconds=60,
    name="activity_public_confirm",
    error_detail="操作過於頻繁，請稍後再試",
)
_public_confirm_limiter = _public_confirm_limiter_instance.as_dependency()


def _verify_parent_identity(
    session, registration_id: int, name: str, birthday: str, parent_phone: str
) -> ActivityRegistration:
    """三欄驗證：name + birthday + parent_phone 與報名一致才回 registration。

    不符一律回 404 且不洩漏是哪一欄錯，維持隱私契約。
    """
    reg = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active.is_(True),
        )
        .first()
    )
    if not reg:
        raise HTTPException(status_code=404, detail="查無對應報名資料")
    normalized = _normalize_phone(parent_phone)
    if (
        reg.student_name != name
        or reg.birthday != birthday
        or _normalize_phone(reg.parent_phone) != normalized
    ):
        raise HTTPException(status_code=404, detail="查無對應報名資料")
    return reg


class _PromotionActionPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    birthday: str = Field(..., min_length=1, max_length=20)
    parent_phone: str = Field(..., min_length=1, max_length=30)


@router.post(
    "/public/registrations/{registration_id}/courses/{course_id}/confirm-promotion"
)
async def public_confirm_promotion(
    registration_id: int,
    course_id: int,
    body: _PromotionActionPayload,
    _: None = Depends(_public_confirm_limiter),
):
    """家長確認接受候補轉正（三欄驗證）。

    錯誤碼：
    - 404：查無對應報名（身份驗證失敗）
    - 409 ALREADY_CONFIRMED：已是正式
    - 409 NOT_PENDING：非待確認狀態（可能已逾期或已放棄）
    - 410 EXPIRED：確認期限已過
    """
    session = get_session()
    try:
        _verify_parent_identity(
            session, registration_id, body.name, body.birthday, body.parent_phone
        )
        try:
            student_name, course_name = activity_service.confirm_waitlist_promotion(
                session, registration_id, course_id
            )
        except ValueError as e:
            code = str(e)
            if code == "NOT_FOUND":
                raise HTTPException(status_code=404, detail="查無對應課程項目")
            if code == "ALREADY_CONFIRMED":
                raise HTTPException(status_code=409, detail="此課程已是正式報名")
            if code == "NOT_PENDING":
                raise HTTPException(
                    status_code=409, detail="此課程非待確認狀態，無法確認"
                )
            if code == "EXPIRED":
                raise HTTPException(
                    status_code=410, detail="確認期限已過，名額已釋出給下一位候補"
                )
            raise
        activity_service.log_change(
            session,
            registration_id,
            student_name,
            "候補轉正確認",
            f"課程「{course_name}」家長確認接受升正式",
            "parent",
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        return {"message": f"已確認升為正式：{course_name}"}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logger.error(
            "候補轉正確認失敗 reg=%s course=%s: %s", registration_id, course_id, e
        )
        raise_safe_500(e)
    finally:
        session.close()


@router.post(
    "/public/registrations/{registration_id}/courses/{course_id}/decline-promotion"
)
async def public_decline_promotion(
    registration_id: int,
    course_id: int,
    body: _PromotionActionPayload,
    _: None = Depends(_public_confirm_limiter),
):
    """家長放棄候補轉正（三欄驗證）。該課程報名會被刪除，遞補下一位。"""
    session = get_session()
    try:
        _verify_parent_identity(
            session, registration_id, body.name, body.birthday, body.parent_phone
        )
        try:
            student_name, course_name = activity_service.decline_waitlist_promotion(
                session, registration_id, course_id, operator="parent"
            )
        except ValueError as e:
            code = str(e)
            if code == "NOT_FOUND":
                raise HTTPException(status_code=404, detail="查無對應課程項目")
            if code == "ALREADY_CONFIRMED":
                raise HTTPException(status_code=409, detail="此課程已是正式報名")
            if code == "NOT_PENDING":
                raise HTTPException(
                    status_code=409, detail="此課程非待確認狀態，無法放棄"
                )
            raise
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        return {"message": f"已放棄升正式：{course_name}"}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logger.error(
            "候補轉正放棄失敗 reg=%s course=%s: %s", registration_id, course_id, e
        )
        raise_safe_500(e)
    finally:
        session.close()


@router.post("/public/inquiries", status_code=201)
async def public_create_inquiry(
    body: PublicInquiryPayload,
    _: None = Depends(_public_inquiry_limiter),
):
    """前台：提交家長提問

    LOW-4：honeypot + 時序檢查若命中 → silent reject（回偽裝成功訊息、不寫 DB）。
    """
    if should_silent_reject_bot(body.hp, body.ts):
        logger.warning(
            "public_create_inquiry silent-reject (honeypot/ts) name=%r phone=%r",
            body.name,
            body.phone,
        )
        return {"message": "感謝您的提問，我們會儘快回覆您！"}
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
        raise_safe_500(e)
    finally:
        session.close()
