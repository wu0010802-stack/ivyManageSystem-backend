"""
api/activity/courses.py — 課程管理端點（5 個）
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError

from models.database import (
    get_session,
    ActivityCourse,
    RegistrationCourse,
    ActivityRegistration,
)
from sqlalchemy import func
from services.activity_service import activity_service
from utils.activity_constants import effective_capacity
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
    has_payment_approve,
    require_approve_for_high_price,
)

from schemas.activity_admin import (
    CourseListOut,
    CourseDetailOut,
    CourseCreateResultOut,
    CoursesCopyResultOut,
    CourseWaitlistOut,
    CourseEnrolledOut,
    validate_phase3_ranges,
)
from schemas._common import DeleteResultOut

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/courses", response_model=CourseListOut)
def get_courses(
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
            capacity = effective_capacity(c)
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
                    # Phase 3 — time 序列化為 "HH:MM" 給前端 advisory 使用
                    "min_age_months": c.min_age_months,
                    "max_age_months": c.max_age_months,
                    "meeting_weekday": c.meeting_weekday,
                    "meeting_start_time": (
                        c.meeting_start_time.strftime("%H:%M")
                        if c.meeting_start_time
                        else None
                    ),
                    "meeting_end_time": (
                        c.meeting_end_time.strftime("%H:%M")
                        if c.meeting_end_time
                        else None
                    ),
                    "instructor_name": c.instructor_name,
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


@router.get("/courses/{course_id}", response_model=CourseDetailOut)
def get_course_detail(
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
            "instructor_name": c.instructor_name,
        }
    finally:
        session.close()


@router.post("/courses", status_code=201, response_model=CourseCreateResultOut)
def create_course(
    body: CourseCreate,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """新增課程"""
    require_approve_for_high_price(
        body.price, current_user, label=f"課程「{body.name}」單價"
    )
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
            min_age_months=body.min_age_months,
            max_age_months=body.max_age_months,
            meeting_weekday=body.meeting_weekday,
            meeting_start_time=body.meeting_start_time,
            meeting_end_time=body.meeting_end_time,
            instructor_name=body.instructor_name,
        )
        session.add(course)
        # 並發同名：兩請求 SELECT 都查不到 → 都 add，後到者撞 partial unique index
        # `uq_activity_course_name_term`。捕 IntegrityError 轉乾淨 400（與序列同名
        # 走 L211 早退一致），避免落入 generic except → raise_safe_500（500）。
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise _duplicate_name("課程")
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


@router.post(
    "/courses/copy-from-previous", status_code=201, response_model=CoursesCopyResultOut
)
def copy_courses_from_previous(
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

        # Bug A 修補：複製前先掃描高價課程，對齊 create_course / update_course 守衛。
        # 任一來源課程超過門檻且操作者缺 ACTIVITY_PAYMENT_APPROVE → 整批 403。
        for src in source_courses:
            require_approve_for_high_price(
                src.price,
                current_user,
                label=f"來源課程「{src.name}」單價",
            )

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
            # Bug B 修補：以 savepoint 包覆單筆插入，捕 IntegrityError（並發同名衝突）
            # → rollback savepoint + skipped，對齊「已存在則跳過」既有語意，避免整批回滾。
            try:
                with session.begin_nested():
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
                        # Phase 3 適齡 + 結構化時段也要帶上（與 create_course 對齊；
                        # 漏掉會讓複製出的課程失去前台不適齡/衝堂 advisory 基礎資料）
                        min_age_months=src.min_age_months,
                        max_age_months=src.max_age_months,
                        meeting_weekday=src.meeting_weekday,
                        meeting_start_time=src.meeting_start_time,
                        meeting_end_time=src.meeting_end_time,
                        instructor_name=src.instructor_name,
                    )
                    session.add(new_course)
                    session.flush()
            except IntegrityError:
                # savepoint 已自動回滾，視同「已存在」跳過（只捕 unique 衝突）
                # 其他非 IntegrityError 例外應往外傳播，由外層 except→raise_safe_500 處理
                skipped += 1
                continue
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


@router.put("/courses/{course_id}", response_model=DeleteResultOut)
def update_course(
    course_id: int,
    body: CourseUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """更新課程"""
    if body.price is not None:
        require_approve_for_high_price(
            body.price, current_user, label=f"課程 #{course_id} 新單價"
        )
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
            # 限定同學期 + is_active，與 partial unique index
            # (name, school_year, semester) WHERE is_active 一致；
            # 跨學期同名是允許的，不該在此誤報衝突
            dup = (
                session.query(ActivityCourse)
                .filter(
                    ActivityCourse.name == body.name,
                    ActivityCourse.id != course_id,
                    ActivityCourse.school_year == course.school_year,
                    ActivityCourse.semester == course.semester,
                    ActivityCourse.is_active.is_(True),
                )
                .first()
            )
            if dup:
                raise _duplicate_name("課程")

        update_data = body.model_dump(exclude_unset=True)

        # Finding 1：變更 sessions（總堂數）會改變退費建議基準——清成 NULL 時
        # build_refund_suggestion 直接建議全退，使「實退 vs 系統建議」偏離閘失效；
        # 只有 ACTIVITY_WRITE 的一線員工可藉此先改總堂數、再以「實退≈建議」+ 小額
        # 累積閘繞過 ACTIVITY_PAYMENT_APPROVE 盜退。課程一旦有報名／出席紀錄，變更
        # sessions 須具 ACTIVITY_PAYMENT_APPROVE；無報名的課程可自由調整。
        if "sessions" in update_data and update_data["sessions"] != course.sessions:
            has_registrations = (
                session.query(RegistrationCourse.id)
                .filter(RegistrationCourse.course_id == course_id)
                .first()
                is not None
            )
            if has_registrations and not has_payment_approve(current_user):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "此課程已有報名／出席紀錄，變更總堂數會影響退費計算基準，"
                        "需由具備『才藝課收款簽核』（ACTIVITY_PAYMENT_APPROVE）權限者執行"
                    ),
                )

        # Finding 6：schema validator 只在成對欄位同時出現於 payload 時才比較，但此處
        # 將 patch 覆寫既有資料 → 單獨更新一邊可寫出矛盾範圍（例 min_age>既有 max_age、
        # start 晚於既有 end）。合併 DB 現值後對完整狀態重新驗證。
        def _eff(field):
            return (
                update_data[field] if field in update_data else getattr(course, field)
            )

        try:
            validate_phase3_ranges(
                _eff("min_age_months"),
                _eff("max_age_months"),
                _eff("meeting_start_time"),
                _eff("meeting_end_time"),
            )
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve))

        for k, v in update_data.items():
            setattr(course, k, v)

        # 改名競態：查重 SELECT 與 commit 間另一請求把別筆改成同名，後到者
        # commit 撞 partial unique index `uq_activity_course_name_term`。比照
        # create 端捕 IntegrityError 轉乾淨 400（與 L415 查到時早退一致），
        # 避免落入下方 generic except → raise_safe_500（500）。
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise _duplicate_name("課程")
        _invalidate_activity_dashboard_caches(session)
        return {"message": "課程更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/courses/{course_id}/waitlist", response_model=CourseWaitlistOut)
def get_course_waitlist(
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


@router.get("/courses/{course_id}/enrolled", response_model=CourseEnrolledOut)
def get_course_enrolled(
    course_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得課程正式報名名單（按報名序排列）"""
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
                RegistrationCourse.status == "enrolled",
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
                    "position": idx + 1,
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


@router.delete("/courses/{course_id}", response_model=DeleteResultOut)
def delete_course(
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
