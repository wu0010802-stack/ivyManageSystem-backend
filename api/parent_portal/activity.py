"""api/parent_portal/activity.py — 家長端才藝課（登入版）。

Batch 7 範圍（plan 確認）：
- list courses（依學期過濾）
- 登入版報名：student_id 直接帶、parent_phone 從 Guardian 取、
  match_status='manual'、pending_review=False
- 列出家長所有子女的報名（單次查詢、用 student_id 過濾）
- 候補升正式（promoted_pending → enrolled，由家長確認）
- 報名繳費歷史（read-only；MVP 不含線上金流，員工 operator 欄位不揭露）
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from models.activity import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    ActivitySession,
    ActivitySupply,
    RegistrationCourse,
    RegistrationSupply,
)
from models.database import Guardian, Student
from services.activity_service import activity_service
from schemas._common import OkStatusOut
from services.business_errors.parent import (
    ParentNotAuthorized,
    StudentNotFound,
)
from utils.academic import resolve_academic_term_filters
from utils.advisory_lock import acquire_activity_registration_lock
from utils.auth import require_parent_role
from services.activity_query_token import _generate_query_token, _hash_query_token
from utils.taipei_time import now_taipei_naive
from utils.activity_constants import OCCUPYING_STATUSES, effective_capacity

from ._dependencies import get_parent_db
from models.parent_db import register_parent_post_commit
from ._shared import _assert_student_owned, _get_parent_student_ids
from api.activity._shared import (
    _calc_total_amount,
    _check_registration_open,
    _derive_payment_status,
    _next_session_dates,
)

router = APIRouter(prefix="/activity", tags=["parent-activity"])

# capacity=NULL 視為 30（0 與 None 語意不同：明確 0 表示不開放名額須保留）。
# 收斂到 utils.activity_constants.effective_capacity 單一來源（取代原家長端
# 自有的 DEFAULT_COURSE_CAPACITY + _effective_capacity）。


def _fmt_time(t) -> Optional[str]:
    """Time → "HH:MM"（對齊公開端 /public/courses 序列化；None 維持 None）。"""
    return t.strftime("%H:%M") if t else None


# --- Response schema（補契約：原本這些端點回裸 dict，OpenAPI 無具名 schema → 前端
# codegen 只能拿到 unknown。宣告 response_model 後前端自動下放真型別。欄位順序/名稱
# 與既有 dict 完全對齊，FastAPI 依此驗證/序列化，不改變 wire shape）---
class ParentCourseItemOut(BaseModel):
    id: int
    name: str
    price: Optional[int] = None
    sessions: Optional[int] = None
    capacity: Optional[int] = None
    school_year: Optional[int] = None
    semester: Optional[int] = None
    allow_waitlist: bool
    description: Optional[str] = None
    video_url: Optional[str] = None
    enrolled_count: int
    is_full: bool
    # Phase 3 適齡 + 結構化時段（前台 advisory：不適齡/衝堂警告，不阻擋報名）。
    # 資料早已在 model（models/activity.py:62-66）；公開端已暴露，本批補家長端。
    min_age_months: Optional[int] = None
    max_age_months: Optional[int] = None
    meeting_weekday: Optional[int] = None  # 0=Mon..6=Sun
    meeting_start_time: Optional[str] = None  # "HH:MM"
    meeting_end_time: Optional[str] = None  # "HH:MM"
    instructor_name: Optional[str] = None
    next_session_date: Optional[str] = None  # 下次上課 ISO date（無排程則 None）


class ParentCourseListOut(BaseModel):
    items: list[ParentCourseItemOut]
    total: int


class RegistrationCourseOut(BaseModel):
    registration_course_id: int
    course_id: int
    course_name: str
    status: str
    price_snapshot: Optional[int] = None
    promoted_at: Optional[str] = None
    confirm_deadline: Optional[str] = None
    # 衝堂偵測用：前端比對已報名課程 vs 目錄課程的 weekday+time。
    meeting_weekday: Optional[int] = None
    meeting_start_time: Optional[str] = None
    meeting_end_time: Optional[str] = None


class RegistrationSummaryOut(BaseModel):
    id: int
    student_id: Optional[int] = None
    student_name: Optional[str] = None
    school_year: Optional[int] = None
    semester: Optional[int] = None
    is_paid: bool
    paid_amount: int
    total_amount: int
    outstanding_amount: int
    payment_status: str
    # 已退費累計（type='refund' 未作廢之和）。供前端區分「退過費歸零」vs「從未繳」
    # ——兩者 paid_amount 都可能為 0、payment_status 也相同，靠此欄分辨。
    refunded_amount: int = 0
    match_status: Optional[str] = None
    pending_review: bool
    courses: list[RegistrationCourseOut]


class MyRegistrationsOut(BaseModel):
    items: list[RegistrationSummaryOut]
    total: int


class RegisterOut(RegistrationSummaryOut):
    # #2：明文 query token 僅報名 response 回傳一次（DB 只存 hash），供前端組
    # 「管理我的報名」公開連結。
    query_token: str


class ParentUpcomingSessionOut(BaseModel):
    student_id: Optional[int] = None
    student_name: Optional[str] = None
    course_id: int
    course_name: str
    session_date: str  # ISO date "YYYY-MM-DD"
    meeting_weekday: Optional[int] = None
    meeting_start_time: Optional[str] = None  # "HH:MM"
    meeting_end_time: Optional[str] = None  # "HH:MM"


class ParentUpcomingSessionsOut(BaseModel):
    items: list[ParentUpcomingSessionOut]
    total: int


class RegisterPayload(BaseModel):
    student_id: int = Field(..., gt=0)
    school_year: int = Field(..., ge=100, le=200)  # 民國
    semester: int = Field(..., ge=1, le=2)
    course_ids: list[int] = Field(default_factory=list)
    supply_ids: list[int] = Field(default_factory=list)

    @field_validator("course_ids", "supply_ids")
    @classmethod
    def _dedupe_ids(cls, v: list[int]) -> list[int]:
        # 去重保序：payload 內重複 id 逐筆 insert 會撞 (registration_id, course/supply_id)
        # 唯一鍵 → 裸 500；官方前端不會送重複，但 malformed/直打 API 的 caller 會。
        return list(dict.fromkeys(v))


@router.get("/courses", response_model=ParentCourseListOut)
def list_courses(
    school_year: Optional[int] = Query(None),
    semester: Optional[int] = Query(None, ge=1, le=2),
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    # F2：未帶學期參數時預設「當前學期」（對齊管理端/公開端全模組 resolve_academic_term_filters
    # 慣例），否則回所有 active 課程（含『複製上學期』遺留），前端用第一筆課程決定報名學期
    # 時可能被帶去報舊學期。只給單一參數會 raise 400，與其他端一致。
    sy, sem = resolve_academic_term_filters(school_year, semester)
    q = session.query(ActivityCourse).filter(
        ActivityCourse.is_active == True,
        ActivityCourse.school_year == sy,
        ActivityCourse.semester == sem,
    )
    courses = q.order_by(ActivityCourse.name.asc()).all()

    # 計算每個 course 已報名（enrolled + promoted_pending）人數，用於前端顯示是否額滿
    # registration_courses 受 RLS 隔離只看得到自己；用 SECURITY DEFINER 函式
    # public_count_enrolled(course_id) 取得跨家長真實 count（catalog UI is_full 依此）
    if not courses:
        return {"items": [], "total": 0}
    enrolled_counts = {
        c.id: int(
            session.execute(func.public_count_enrolled(c.id).select()).scalar() or 0
        )
        for c in courses
    }
    next_session_map = _next_session_dates(session, [c.id for c in courses])
    items = [
        {
            "id": c.id,
            "name": c.name,
            "price": c.price,
            "sessions": c.sessions,
            # Finding 5：回 effective 值（NULL→30），與 is_full 的 effective_capacity
            # 口徑一致；否則 NULL 容量課前端顯示 "enrolled/null"。型別仍 Optional[int]，
            # 不改 wire shape / OpenAPI schema。
            "capacity": effective_capacity(c),
            "school_year": c.school_year,
            "semester": c.semester,
            "allow_waitlist": bool(c.allow_waitlist),
            "description": c.description,
            "video_url": c.video_url,
            "enrolled_count": enrolled_counts.get(c.id, 0),
            "is_full": enrolled_counts.get(c.id, 0) >= effective_capacity(c),
            # Phase 3 適齡 + 結構化時段（前台 advisory）；對齊公開端 /public/courses。
            "min_age_months": c.min_age_months,
            "max_age_months": c.max_age_months,
            "meeting_weekday": c.meeting_weekday,
            "meeting_start_time": _fmt_time(c.meeting_start_time),
            "meeting_end_time": _fmt_time(c.meeting_end_time),
            "instructor_name": c.instructor_name,
            "next_session_date": next_session_map.get(c.id),
        }
        for c in courses
    ]
    return {"items": items, "total": len(items)}


def _registration_summary(session, reg: ActivityRegistration) -> dict:
    """組合 registration 與 enrolled/waitlist courses 摘要。"""
    courses = (
        session.query(RegistrationCourse, ActivityCourse)
        .join(ActivityCourse, ActivityCourse.id == RegistrationCourse.course_id)
        .filter(RegistrationCourse.registration_id == reg.id)
        .all()
    )
    # ④ 直接回傳金額口徑，前端不再自行加總（避免漏扣已繳、誤計候補課程、漏算用品）。
    # total_amount 只計 enrolled 課程 + 用品（候補不計），與 _derive_payment_status /
    # 後台 / 公開端口徑一致。
    paid_amount = reg.paid_amount or 0
    total_amount = _calc_total_amount(session, reg.id)
    # 已退費累計：type='refund' 且未作廢之和（amount 恆正，方向由 type 區分）。
    refunded_amount = int(
        session.query(func.coalesce(func.sum(ActivityPaymentRecord.amount), 0))
        .filter(
            ActivityPaymentRecord.registration_id == reg.id,
            ActivityPaymentRecord.type == "refund",
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .scalar()
        or 0
    )
    return {
        "id": reg.id,
        "student_id": reg.student_id,
        "student_name": reg.student_name,
        "school_year": reg.school_year,
        "semester": reg.semester,
        "is_paid": bool(reg.is_paid),
        "paid_amount": paid_amount,
        "total_amount": total_amount,
        "outstanding_amount": max(total_amount - paid_amount, 0),
        "payment_status": _derive_payment_status(paid_amount, total_amount),
        "refunded_amount": refunded_amount,
        "match_status": reg.match_status,
        "pending_review": bool(reg.pending_review),
        "courses": [
            {
                "registration_course_id": rc.id,
                "course_id": rc.course_id,
                "course_name": c.name,
                "status": rc.status,
                "price_snapshot": rc.price_snapshot,
                "promoted_at": rc.promoted_at.isoformat() if rc.promoted_at else None,
                "confirm_deadline": (
                    rc.confirm_deadline.isoformat() if rc.confirm_deadline else None
                ),
                # 衝堂偵測：帶課程時段，前端比對已報名 vs 目錄課程。
                "meeting_weekday": c.meeting_weekday,
                "meeting_start_time": _fmt_time(c.meeting_start_time),
                "meeting_end_time": _fmt_time(c.meeting_end_time),
            }
            for rc, c in courses
        ],
    }


@router.get("/my-registrations", response_model=MyRegistrationsOut)
def my_registrations(
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    user_id = current_user["user_id"]
    _, student_ids = _get_parent_student_ids(session, user_id)
    if not student_ids:
        return {"items": [], "total": 0}
    rows = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.student_id.in_(student_ids),
            ActivityRegistration.is_active == True,
        )
        .order_by(ActivityRegistration.created_at.desc())
        .all()
    )
    return {
        "items": [_registration_summary(session, r) for r in rows],
        "total": len(rows),
    }


@router.get("/upcoming-sessions", response_model=ParentUpcomingSessionsOut)
def upcoming_sessions(
    days: int = Query(30, ge=1, le=90),
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    """家長子女『已佔位』（enrolled / promoted_pending）課程未來 days 天內的場次。

    finding #2：原 hero upcomingCount 固定 0（course response 無起訖日）。正解是查
    ActivitySession（逐場 session_date），非補 course.start_date（model 無此欄）。
    前端據此算 upcomingCount（7 天內）與各課『下次上課』。候補課程未佔位故不計；
    過去場次（session_date < 今日台灣時間）排除。場次無時間欄，時間取自課程。
    """
    user_id = current_user["user_id"]
    _, student_ids = _get_parent_student_ids(session, user_id)
    if not student_ids:
        return {"items": [], "total": 0}
    today = now_taipei_naive().date()
    end = today + timedelta(days=days)
    rows = (
        session.query(ActivitySession, ActivityCourse, ActivityRegistration)
        .join(ActivityCourse, ActivityCourse.id == ActivitySession.course_id)
        .join(RegistrationCourse, RegistrationCourse.course_id == ActivityCourse.id)
        .join(
            ActivityRegistration,
            ActivityRegistration.id == RegistrationCourse.registration_id,
        )
        .filter(
            ActivityRegistration.student_id.in_(student_ids),
            ActivityRegistration.is_active == True,
            RegistrationCourse.status.in_(OCCUPYING_STATUSES),
            ActivitySession.session_date >= today,
            ActivitySession.session_date <= end,
        )
        .order_by(ActivitySession.session_date.asc(), ActivityCourse.name.asc())
        .all()
    )
    items = [
        {
            "student_id": reg.student_id,
            "student_name": reg.student_name,
            "course_id": course.id,
            "course_name": course.name,
            "session_date": sess.session_date.isoformat(),
            "meeting_weekday": course.meeting_weekday,
            "meeting_start_time": _fmt_time(course.meeting_start_time),
            "meeting_end_time": _fmt_time(course.meeting_end_time),
        }
        for sess, course, reg in rows
    ]
    return {"items": items, "total": len(items)}


@router.post("/register", status_code=201, response_model=RegisterOut)
def register_courses(
    payload: RegisterPayload,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    """登入版報名：student_id 必為自己小孩、parent_phone 自動從 Guardian 帶入。"""
    if not payload.course_ids and not payload.supply_ids:
        raise HTTPException(status_code=400, detail="至少需選擇一門課程或一項用品")

    # ② 登入家長報名同樣受報名開放時間限制（比照公開端 public_register）。
    # 否則後台關閉報名 / 已截止後，登入家長仍可直接打 API 繞過時段。
    _check_registration_open(session)

    user_id = current_user["user_id"]
    _assert_student_owned(session, user_id, payload.student_id, for_write=True)

    student = session.query(Student).filter(Student.id == payload.student_id).first()
    if student is None:
        raise StudentNotFound("找不到學生")

    # L2：以「同學生身分 + 學期」序列化並發報名的 check-then-insert。DB partial
    # unique 鍵含 parent_phone 不含 student_id，兩位不同 Guardian（phone 不同）並發
    # 替同一學生報名會雙雙通過下方 existing 檢查、index 也攔不住 → 長出兩筆有效
    # 報名（容量重複佔用、在籍灌水）。比照 admin match 取報名 advisory lock；
    # SQLite no-op，真正序列化由 PostgreSQL pg_advisory_xact_lock 提供。
    acquire_activity_registration_lock(
        session,
        student_name=student.name,
        birthday=(student.birthday.isoformat() if student.birthday else ""),
        school_year=payload.school_year,
        semester=payload.semester,
    )

    # 防同學期重複報名
    existing = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.student_id == payload.student_id,
            ActivityRegistration.school_year == payload.school_year,
            ActivityRegistration.semester == payload.semester,
            ActivityRegistration.is_active == True,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=400, detail="該學期已有活的報名，請先取消既有報名再重新提交"
        )

    guardian = (
        session.query(Guardian)
        .filter(
            Guardian.user_id == user_id,
            Guardian.student_id == payload.student_id,
            Guardian.deleted_at.is_(None),
        )
        .order_by(Guardian.is_primary.desc())
        .first()
    )
    parent_phone = guardian.phone if guardian else None

    # #2：家長端登入報名也比照公開報名（public_register）產生 query token。
    # 否則此報名 query_token_hash IS NULL → 公開破壞性 mutation 的身分驗證
    # （_parent_mutation_identity_ok）退回姓名+生日+電話三欄，知道這三項 PII 的
    # 陌生人即可未登入改課程/放棄候補。寫入 hash 後公開 mutation 強制有效 token；
    # 明文 token 只在本次 response 回給家長一次（供「管理我的報名」連結，留存自助修改能力）。
    plaintext_token = _generate_query_token()

    reg = ActivityRegistration(
        student_name=student.name,
        birthday=(student.birthday.isoformat() if student.birthday else None),
        class_name=None,  # 由 classroom_id 解析；不寫快照避免老資料
        email=None,
        is_paid=False,
        paid_amount=0,
        is_active=True,
        school_year=payload.school_year,
        semester=payload.semester,
        student_id=student.id,
        parent_phone=parent_phone,
        classroom_id=student.classroom_id,
        pending_review=False,  # 登入版視為已驗證
        match_status="manual",
        query_token_hash=_hash_query_token(plaintext_token),
        query_token_issued_at=now_taipei_naive(),
    )
    session.add(reg)
    session.flush()

    # ① 對本次報名涉及的課程列加行鎖（with_for_update），與公開端 public_register
    # 相同的鎖定策略：序列化「讀容量 → 判 enrolled/waitlist → 寫入」，避免並發報名
    # 同時讀到最後名額都寫成 enrolled 造成超賣。一次以 id 排序整批鎖定（而非逐課
    # 迴圈鎖），避免兩個請求以不同順序鎖多課程互等的死結。SQLite 下 FOR UPDATE
    # 為 no-op（同 public_register），真正序列化由 PostgreSQL 行鎖提供。
    # F1：限定同學期課程，避免把舊學期 active 課程掛到新學期報名（對齊公開/後台端）。
    locked_courses: dict[int, ActivityCourse] = {}
    if payload.course_ids:
        locked_courses = {
            c.id: c
            for c in session.query(ActivityCourse).filter(
                ActivityCourse.id.in_(sorted(payload.course_ids)),
                ActivityCourse.is_active == True,
                ActivityCourse.school_year == payload.school_year,
                ActivityCourse.semester == payload.semester,
            )
            # 以 id 排序固定 FOR UPDATE 列鎖的取得順序：id.in_(sorted(...)) 只排了
            # Python 端 IN 清單，不決定列鎖順序（由查詢計畫決定）；缺 ORDER BY 時
            # 兩交易以不同順序鎖重疊課程仍會 ABBA。order_by 須在 with_for_update 前。
            .order_by(ActivityCourse.id).with_for_update().all()
        }

    # 加入課程：依容量決定 enrolled / waitlist。容量檢查同樣需用 admin-bypass
    # SECURITY DEFINER function 取真實 count（RLS 隔離下只看自己會誤判沒滿）
    for course_id in payload.course_ids:
        # 學期不符 / 不存在即視為查無此課，沿用既有「找不到課程」400 分支。
        course = locked_courses.get(course_id)
        if course is None:
            raise HTTPException(status_code=400, detail=f"找不到課程 id={course_id}")
        enrolled_count = int(
            session.execute(func.public_count_enrolled(course_id).select()).scalar()
            or 0
        )
        if enrolled_count < effective_capacity(course):
            status = "enrolled"
        elif course.allow_waitlist:
            status = "waitlist"
        else:
            raise HTTPException(
                status_code=400,
                detail=f"課程「{course.name}」已額滿且不開放候補",
            )
        session.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course_id,
                status=status,
                price_snapshot=course.price or 0,
            )
        )

    for supply_id in payload.supply_ids:
        # F1：限定同學期用品（對齊公開/後台端），學期不符視為查無此用品。
        supply = (
            session.query(ActivitySupply)
            .filter(
                ActivitySupply.id == supply_id,
                ActivitySupply.is_active == True,
                ActivitySupply.school_year == payload.school_year,
                ActivitySupply.semester == payload.semester,
            )
            .first()
        )
        if supply is None:
            raise HTTPException(status_code=400, detail=f"找不到用品 id={supply_id}")
        session.add(
            RegistrationSupply(
                registration_id=reg.id,
                supply_id=supply_id,
                price_snapshot=supply.price or 0,
            )
        )

    session.flush()
    session.refresh(reg)
    # F4：家長端報名改變 enrolled 集合 → 清 dashboard 快取（原本家長端完全不清）。
    # P2（2026-06-23 code review）：延後到 parent 交易 commit 後才清。家長 handler 不可
    # commit（RLS），commit 由 get_parent_db dependency 負責；若在此 flush 後即清快取
    # （report_cache_service 用獨立 session 立即 commit），並發 dashboard 讀取會在本筆
    # 報名尚未 commit 的窗口內以 pre-commit stale 資料重建快取並續存 TTL(1800s)。
    register_parent_post_commit(
        session, lambda: activity_service.invalidate_dashboard_caches(None)
    )
    # #2：明文 query token 僅此 response 回傳一次（DB 只存 hash），供前端組「管理我的
    # 報名」公開連結；後續查詢/列表不再回傳（無法由 hash 還原）。
    return {**_registration_summary(session, reg), "query_token": plaintext_token}


class ConfirmPromotionPayload(BaseModel):
    course_id: int = Field(..., gt=0)


@router.post(
    "/registrations/{registration_id}/confirm-promotion", response_model=OkStatusOut
)
def confirm_promotion(
    registration_id: int,
    payload: ConfirmPromotionPayload,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    """家長確認候補升正式：promoted_pending → enrolled。"""
    user_id = current_user["user_id"]
    # F-003：「報名不存在」「未綁定學生」「不屬於本家庭」一律 generic 403，
    # 避免透過 status code/detail 差異枚舉 ActivityRegistration id 存在性。
    _, owned_student_ids = _get_parent_student_ids(session, user_id)
    reg = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active == True,
        )
        .first()
    )
    if reg is None or reg.student_id is None or reg.student_id not in owned_student_ids:
        raise ParentNotAuthorized("查無此資料或無權存取")

    # 改用 services.activity_service.confirm_waitlist_promotion：
    # 與公開端 api/activity/public.py 共用同一 helper（已加 with_for_update
    # on rc + course）。原 parent_portal 自寫 SELECT-then-UPDATE 無鎖、
    # 沒呼叫 log_change，與公開端行為不對稱；雙裝置並發確認時兩個 commit
    # 都會 200 而沒留稽核軌跡（bug sweep round 5 P2，2026-05-14）。
    try:
        student_name, course_name = activity_service.confirm_waitlist_promotion(
            session, registration_id, payload.course_id
        )
    except ValueError as e:
        code = str(e)
        if code == "NOT_FOUND":
            raise HTTPException(status_code=404, detail="找不到該報名課程")
        if code == "ALREADY_CONFIRMED":
            raise HTTPException(status_code=409, detail="此課程已是正式報名")
        if code == "NOT_PENDING":
            raise HTTPException(status_code=400, detail="此課程非待確認狀態，無法確認")
        if code == "EXPIRED":
            raise HTTPException(
                status_code=410, detail="確認期限已過，名額已釋出給下一位候補"
            )
        if code == "STUDENT_TERMINAL":
            raise HTTPException(
                status_code=403,
                detail="此學生已離校，無法升為正式；可繼續查看歷史紀錄",
            )
        raise
    # 與公開端 api/activity/public.py 對齊：confirm 後寫一筆業務 audit 軌跡，
    # operator 標 "parent" 區別於公開頁的 "parent-public"。
    activity_service.log_change(
        session,
        registration_id,
        student_name,
        "候補轉正確認",
        f"課程「{course_name}」家長確認接受升正式（parent-portal）",
        "parent",
    )
    session.flush()
    # F4：轉正改變 enrolled 集合 → 清 dashboard 快取。
    # P2（2026-06-23 code review）：延後到 parent 交易 commit 後才清（同 register_courses，
    # 避免在 commit 前清快取造成並發讀者以 stale 資料重建並續存 TTL）。
    register_parent_post_commit(
        session, lambda: activity_service.invalidate_dashboard_caches(None)
    )
    return {"status": "ok"}


@router.get("/registrations/{registration_id}/payments")
def registration_payments(
    registration_id: int,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    """報名繳費歷史；不揭露 operator 等員工欄位。"""
    user_id = current_user["user_id"]
    # F-003：「報名不存在」「未綁定學生」「不屬於本家庭」一律 generic 403。
    _, owned_student_ids = _get_parent_student_ids(session, user_id)
    reg = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active == True,
        )
        .first()
    )
    if reg is None or reg.student_id is None or reg.student_id not in owned_student_ids:
        raise ParentNotAuthorized("查無此資料或無權存取")

    rows = (
        session.query(ActivityPaymentRecord)
        .filter(
            ActivityPaymentRecord.registration_id == registration_id,
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .order_by(
            ActivityPaymentRecord.payment_date.asc(),
            ActivityPaymentRecord.id.asc(),
        )
        .all()
    )
    return {
        "registration_id": registration_id,
        "items": [
            {
                "type": r.type,
                "amount": r.amount,
                "payment_date": (
                    r.payment_date.isoformat() if r.payment_date else None
                ),
                "payment_method": r.payment_method,
                "receipt_no": r.receipt_no,
            }
            for r in rows
        ],
    }
