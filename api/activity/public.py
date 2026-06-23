"""
api/activity/public.py — 公開前台端點（無需認證，10 個）
"""

import time
import logging
import random
from datetime import datetime
from utils.taipei_time import now_taipei_naive
from pathlib import Path

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import Response as PlainResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from utils.cache_layer import get_cache

_CACHE_NS_PUBLIC_AVAILABILITY = "public_availability"
_CACHE_KEY_AVAILABILITY = "all"
_CACHE_TTL_AVAILABILITY = 10  # seconds — advisory display only, true overbooking guard is register with_for_update

from utils.errors import raise_safe_500
from utils.audit import write_explicit_audit

_POSTER_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_POSTER_MODULE = "activity_posters"


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
from utils.rate_limit import create_limiter

from schemas.activity_public import (
    PublicRegistrationTimeOut,
    PublicCoursesItemOut,
    PublicSuppliesItemOut,
    PublicRegistrationDetailOut,
    PublicRegisterResultOut,
)
from schemas._common import DeleteResultOut

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
    _invalidate_after_registration_mutation,
    _derive_payment_status,
    _check_registration_open,
    _attach_courses,
    _attach_supplies,
    _calc_total_amount,
    _next_session_dates,
    _compute_is_paid,
    _match_student_with_parent_phone,
    _normalize_phone,
    _public_etag_response,
    _resolve_class_field_state,
    _build_public_query_payload,
    _generate_query_token,
    _hash_query_token,
    is_query_token_expired,
    TAIPEI_TZ,
)
from utils.academic import resolve_academic_term_filters

logger = logging.getLogger(__name__)
router = APIRouter()

_public_query_limiter_instance = create_limiter(
    max_calls=10,
    window_seconds=60,
    name="activity_public_query",
    error_detail="查詢過於頻繁，請稍後再試",
)
_public_query_limiter = _public_query_limiter_instance.as_dependency()

_public_register_limiter_instance = create_limiter(
    max_calls=5,
    window_seconds=60,
    name="activity_public_register",
    error_detail="提交過於頻繁，請稍後再試",
)
_public_register_limiter = _public_register_limiter_instance.as_dependency()

# 家長提問：相較報名放寬一些，避免誤擋連續補充問題
_public_inquiry_limiter_instance = create_limiter(
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


@router.get("/public/registration-time", response_model=PublicRegistrationTimeOut)
def get_public_registration_time(request: Request, response: Response):
    """公開端點：前台查詢報名開放時間 + 顯示設定（無需認證）"""
    session = get_session()
    try:
        settings = session.query(ActivityRegistrationSettings).first()
        if not settings:
            # Finding 1（2026-06-22）：無 settings 列時 _check_registration_open
            # 放行報名（業主裁：維持放行）。此處須回 is_open=True 與其一致，
            # 否則 UI 顯示關閉但 API 實際開放，可繞過前台直接報名。
            payload = {
                "is_open": True,
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
def get_public_poster(filename: str, response: Response):
    """公開端點：回傳已上傳的活動海報圖。

    防穿越：檔名只允許純 hex + 白名單副檔名。
    backend 為 local：直接 stream bytes；supabase：302 redirect 到 CDN URL。
    """
    from fastapi.responses import RedirectResponse
    from utils.storage import LocalStorage, get_backend

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

    backend = get_backend()
    if not backend.exists(_POSTER_MODULE, filename):
        raise HTTPException(status_code=404, detail="海報不存在")

    if isinstance(backend, LocalStorage):
        # local：直接吐 bytes 維持 e2e 測試簡單
        data = backend.read(_POSTER_MODULE, filename)
        return PlainResponse(
            content=data,
            media_type=_POSTER_MIME.get(ext, "image/*"),
            headers={"Cache-Control": "public, max-age=300"},
        )

    # supabase：redirect 到 CDN URL（瀏覽器後續直接從 Supabase 拿）
    url = backend.public_url(_POSTER_MODULE, filename)
    return RedirectResponse(url, status_code=302)


@router.get("/public/courses", response_model=list[PublicCoursesItemOut])
def get_public_courses(request: Request, response: Response):
    """前台：取得課程列表"""
    session = get_session()
    try:
        # E1：只回當學期課程，避免上一學期/「複製上學期」遺留的 is_active 課程
        # 在公開報名頁同名重複列出（register 寫入路徑本就過濾學期，讀取端對齊）。
        sy, sem = resolve_academic_term_filters(None, None, session)
        courses = (
            session.query(ActivityCourse)
            .filter(
                ActivityCourse.is_active.is_(True),
                ActivityCourse.school_year == sy,
                ActivityCourse.semester == sem,
            )
            .order_by(ActivityCourse.id)
            .all()
        )
        next_session_map = _next_session_dates(session, [c.id for c in courses])
        payload = [
            {
                "name": c.name,
                "price": c.price,
                "sessions": c.sessions,
                "frequency": "",
                # Phase 3 — time 序列化為 "HH:MM" 給家長公開報名頁 advisory
                "min_age_months": c.min_age_months,
                "max_age_months": c.max_age_months,
                "meeting_weekday": c.meeting_weekday,
                "meeting_start_time": (
                    c.meeting_start_time.strftime("%H:%M")
                    if c.meeting_start_time
                    else None
                ),
                "meeting_end_time": (
                    c.meeting_end_time.strftime("%H:%M") if c.meeting_end_time else None
                ),
                "instructor_name": c.instructor_name,
                "next_session_date": next_session_map.get(c.id),
            }
            for c in courses
        ]
        return _public_etag_response(request, response, payload)
    finally:
        session.close()


@router.get("/public/supplies", response_model=list[PublicSuppliesItemOut])
def get_public_supplies(request: Request, response: Response):
    """前台：取得用品列表"""
    session = get_session()
    try:
        # E1：只回當學期用品（同 /public/courses 理由）。
        sy, sem = resolve_academic_term_filters(None, None, session)
        supplies = (
            session.query(ActivitySupply)
            .filter(
                ActivitySupply.is_active.is_(True),
                ActivitySupply.school_year == sy,
                ActivitySupply.semester == sem,
            )
            .order_by(ActivitySupply.id)
            .all()
        )
        payload = [{"name": s.name, "price": s.price} for s in supplies]
        return _public_etag_response(request, response, payload)
    finally:
        session.close()


@router.get("/public/classes", response_model=list[str])
def get_public_classes(request: Request, response: Response):
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


def _compute_availability(session) -> dict:
    """聚合計算各課程剩餘名額。抽成獨立函式供快取層包覆及測試 spy。

    佔容量 = enrolled + promoted_pending（兩者皆已佔名額，避免超發候補通知）。
    """
    # E1：只計當學期課程，避免跨學期同名課以 course.name 當 key 互相碰撞、
    # 顯示錯學期的剩餘名額。
    sy, sem = resolve_academic_term_filters(None, None, session)
    courses = (
        session.query(ActivityCourse)
        .filter(
            ActivityCourse.is_active.is_(True),
            ActivityCourse.school_year == sy,
            ActivityCourse.semester == sem,
        )
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
    return availability


@router.get("/public/courses/availability", response_model=dict[str, int])
def get_public_courses_availability(request: Request, response: Response):
    """前台：取得課程名額狀況（帶 10s TTL 記憶體快取降低 DB 壓力）。

    快取設計：
    - availability 為 advisory 顯示；真正防超賣靠 register 端點的 with_for_update
    - bounded staleness 10s 自然過期；register/update 可選 clear_namespace 加速反映
    - cache hit 完全跳過 DB session，節省連線 + 兩次 query 開銷
    - ETag/304 邏輯不動，對快取結果做 md5 (md5 over small dict 可忽略)
    """
    cache = get_cache()
    availability = cache.get(_CACHE_NS_PUBLIC_AVAILABILITY, _CACHE_KEY_AVAILABILITY)
    if availability is None:
        session = get_session()
        try:
            availability = _compute_availability(session)
        finally:
            session.close()
        cache.set(
            _CACHE_NS_PUBLIC_AVAILABILITY,
            _CACHE_KEY_AVAILABILITY,
            availability,
            ttl=_CACHE_TTL_AVAILABILITY,
        )
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


@router.get("/public/course-videos", response_model=dict[str, str])
def get_public_course_videos(request: Request, response: Response):
    """前台：取得課程介紹影片 URL"""
    session = get_session()
    try:
        # C8：只回當學期課程影片。否則跨學期同名課（含「複製上學期」遺留）以 course.name
        # 當 dict key 互相覆寫，且非當學期影片外洩到當學期報名頁（與 /public/courses 對齊）。
        sy, sem = resolve_academic_term_filters(None, None, session)
        courses = (
            session.query(ActivityCourse)
            .filter(
                ActivityCourse.is_active.is_(True),
                ActivityCourse.school_year == sy,
                ActivityCourse.semester == sem,
                ActivityCourse.video_url.isnot(None),
                ActivityCourse.video_url != "",
            )
            .all()
        )
        payload = {c.name: c.video_url for c in courses}
        return _public_etag_response(request, response, payload)
    finally:
        session.close()


class _PublicQueryPayload(BaseModel):
    """POST body for /public/query — 姓名+生日+家長手機查詢報名。

    改為 POST body（原 GET query params）— 避免 PII 進 access log /
    瀏覽器歷史 / Referer（與 /public/query-by-token 同理）。

    schema 不設嚴格 max_length 以免 422 vs 404 status code 差異洩漏 oracle；
    parent_phone max_length=30 防 DoS 級超長 payload（與 _PublicQueryByTokenPayload 一致）。
    """

    name: str = Field(..., min_length=1, max_length=50)
    birthday: str = Field(..., min_length=8, max_length=20)
    parent_phone: str = Field(..., min_length=8, max_length=30)


@router.post("/public/query", response_model=PublicRegistrationDetailOut)
def public_query_registration(
    body: _PublicQueryPayload,
    _: None = Depends(_public_query_limiter),
):
    """前台：依姓名+生日+家長手機查詢報名資料（POST body，避免 PII 進 URL）

    三欄必須同時相符；任一欄不符一律回相同的通用錯誤（不洩漏是哪一欄不符）。

    POST 而非 GET — 避免姓名/生日/家長手機進 access log / 瀏覽器歷史 / Referer，
    與 /public/query-by-token 隱私契約一致。

    LOW-3：對成功與失敗 path 加入 200~500ms 隨機延遲，提高低成本枚舉成本。
    """
    time.sleep(random.uniform(0.2, 0.5))
    session = get_session()
    try:
        normalized_phone = _normalize_phone(body.parent_phone)
        # 先抓 (name, birthday) 候選（同姓同生日通常極少），再統一在 Python 端
        # 比對 normalize 後的 phone；無論是否匹配都走相同程式路徑，壓低時序差。
        # order_by 讓多筆跨學期 active 報名（同名同生日同手機可在不同學期各一筆，
        # partition unique index per-term 允許）的取捨 deterministic：取最新學期，
        # 避免依 DB 預設順序任意取到舊學期那筆。
        candidates = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.student_name == body.name,
                ActivityRegistration.birthday == body.birthday,
                ActivityRegistration.is_active.is_(True),
            )
            .order_by(
                ActivityRegistration.school_year.desc(),
                ActivityRegistration.semester.desc(),
                ActivityRegistration.id.desc(),
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

        return _build_public_query_payload(session, reg)
    finally:
        session.close()


class _PublicQueryByTokenPayload(BaseModel):
    """以查詢碼 + 家長手機查詢報名（Phase 3）。

    威脅模型：token 是 convenience layer（家長拿到後免記憶/換手機後仍能查），
    不是 security layer。phone 仍是必要第二因素，避免 token 從家長 LINE 截圖
    被轉傳後直接被陌生人讀取資料。

    schema 故意不設 min_length — 422 與 404 的 status code 差異會洩漏「token 是否
    合法格式」的 oracle。攻擊者就算送 1 char token，後端 hash 也比不上，回統一 404。
    max_length 保留是為防 DoS 級超長 payload。
    """

    token: str = Field(..., min_length=1, max_length=256)
    parent_phone: str = Field(..., min_length=8, max_length=30)


@router.post("/public/query-by-token", response_model=PublicRegistrationDetailOut)
def public_query_by_token(
    body: _PublicQueryByTokenPayload,
    _: None = Depends(_public_query_limiter),
):
    """前台：以查詢碼（明文 token）+ 家長手機查詢報名資料（Phase 3）。

    與三欄查詢（/public/query）並存：既有報名沒有 token，沿用三欄；新報名 register
    response 拿到的 token 走此端點。POST 而非 GET — 避免 token 進 access log /
    瀏覽器歷史 / referer。

    不符一律回 404 同訊息（與 /public/query 隱私契約一致），不洩漏「token 不存在」
    與「token 對 phone 錯」的差別。

    LOW-3 一致性：成功與失敗 path 都加入隨機延遲，壓低時序差。
    """
    time.sleep(random.uniform(0.2, 0.5))
    session = get_session()
    try:
        token_hash = _hash_query_token(body.token)
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.query_token_hash == token_hash,
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        # 資安 P0 (2026-05-07)：查詢碼到期判定。issued_at 為 None（舊資料）或超過 TTL
        # 一律回 404 同訊息（與 token 不存在 / phone 錯一致），不洩漏「token 過期」
        # 與其他失敗的差別。家長過期後自然引導到 /public/query 三欄比對。
        token_expired = reg is not None and is_query_token_expired(
            reg.query_token_issued_at
        )
        if (
            reg is None
            or token_expired
            or _normalize_phone(reg.parent_phone) != _normalize_phone(body.parent_phone)
        ):
            raise HTTPException(
                status_code=404,
                detail="查無對應報名，請確認查詢碼與手機號碼是否正確",
            )
        return _build_public_query_payload(session, reg)
    finally:
        session.close()


@router.post(
    "/public/register", status_code=201, response_model=PublicRegisterResultOut
)
def public_register(
    body: PublicRegistrationPayload,
    _: None = Depends(_public_register_limiter),
):
    """前台：提交報名表（分學期、靜默比對在校生、失敗進入待審核佇列）。

    隱私契約：response 絕不洩漏 match_status / classroom_id / student_id /
    pending_review 等任何比對結果；成功/失敗家長看到同樣的中性訊息。

    LOW-4：honeypot + 時序檢查若命中 → silent reject（回偽裝成功訊息、不寫 DB）。
    """
    # Phase 3：silent path 也要回 query_token shape，否則攻擊者可從「response 有沒有
    # query_token 欄位」反推這次提交是真的成功還是 silent-reject，F-030 的 oracle 又回來。
    # silent path 的 token 是「即拋型」— 不寫 DB，家長拿去查也會 404（與真正失敗一致）。
    _silent_query_token = _generate_query_token()

    if should_silent_reject_bot(body.hp, body.ts):
        logger.warning(
            "public_register silent-reject (honeypot/ts)",
        )
        return {
            "message": "報名資料已送出，校方將於 1-2 個工作天確認後主動與您聯繫。",
            "id": 0,
            "waitlisted": False,
            "waitlist_courses": [],
            "query_token": _silent_query_token,
        }
    # F-030：silent-success（與 honeypot 路徑一致）的中性回應，
    # 攻擊者無法從重複/驗證失敗中分辨存在性。
    _silent_success_response = {
        "message": "報名資料已送出，校方將於 1-2 個工作天確認後主動與您聯繫。",
        "id": 0,
        "waitlisted": False,
        "waitlist_courses": [],
        "query_token": _silent_query_token,
    }

    session = get_session()
    try:
        _check_registration_open(session)

        # 決定學期（未傳則用當前）
        sy, sem = resolve_academic_term_filters(body.school_year, body.semester)

        # F-030：先做三欄靜默比對，再決定重複報名訊息要 raise 400（已驗身分的家長）
        # 還是 silent-success（未通過比對的潛在 enumeration probe）。
        # 舊實作把 existing/pending_dup 兩支 SELECT 放在 _match_student_with_parent_phone
        # 之前 + 命中即 raise 400 → 未登入攻擊者可用任意 (student_name, birthday) /
        # parent_phone 做存在性 oracle。改為先驗身分後再分流，攻擊者只看到統一的
        # silent-success 回應，無法區分「該學生有有效報名」、「該電話有 pending」、
        # 「真正的新報名」三種情況。
        matched_student_id, matched_classroom_id = _match_student_with_parent_phone(
            session, body.name, body.birthday, body.parent_phone
        )
        is_matched_for_dup_check = bool(matched_student_id and matched_classroom_id)

        # 重複報名防護（同學期內同學生不可重複）
        # P2-5：dedup 鍵須含 parent_phone，與 DB partial unique index
        # uq_activity_regs_student_term_active 的 (name,birthday,sy,sem,parent_phone)
        # 對齊。否則同名同生日但不同家長電話的第二個合法家庭會被誤判重複而 raise/
        # 靜默吞掉（未匹配身分者走 silent-success → 假成功、DB 沒寫入）。
        existing = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.student_name == body.name,
                ActivityRegistration.birthday == body.birthday,
                ActivityRegistration.parent_phone == body.parent_phone,
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.school_year == sy,
                ActivityRegistration.semester == sem,
            )
            .first()
        )
        if existing:
            if is_matched_for_dup_check:
                # 已驗證身分的家長：保留明確 UX，方便他們改用「修改功能」
                raise HTTPException(
                    status_code=400,
                    detail="此學生本學期已有有效報名，請使用修改功能",
                )
            # 未驗證身分（含枚舉攻擊者）：silent-success，不寫 DB、不洩漏存在性
            return _silent_success_response

        # Finding 2（2026-06-22）：原本此處還有一段 phone-only soft-dedup
        # （同 parent_phone + 學期若已有任一 pending 即擋），會把「手足共用家長
        # 電話」的第二個孩子（不同 name/birthday）誤判重複而靜默丟棄（silent-success
        # 假成功、DB 沒寫入）。業主裁：手足應可各自報名。
        # 加上 name+birthday 後，此 dedup 與上方 `existing` 檢查（name+birthday+
        # phone+學期、is_active 含 pending）完全重疊變死碼，故整段移除——同一學生
        # 重送一律由 `existing` 攔下（已驗證身分→400、未驗證→silent-success），
        # 不同學生（手足）則正常各自寫入。氾濫送件仍由 register rate limiter 控制。

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

        # Phase 3：產明文 query token，hash 寫進 DB；明文只在這次 response 回給家長一次
        # 資安 P0 (2026-05-07)：同時記 issued_at，180 天後 token 自動失效
        plaintext_token = _generate_query_token()
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
            query_token_hash=_hash_query_token(plaintext_token),
            query_token_issued_at=now_taipei_naive(),
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
            # F-030：與 in-Python existing/pending_dup 檢查同樣分流——已驗證身分的家長
            # 看到 400 明確訊息；未驗證身分（潛在攻擊者）一律 silent-success，不再
            # 透過 race-condition 路徑洩漏存在性 oracle。
            session.rollback()
            msg_lower = str(getattr(ie, "orig", ie)).lower()
            if "uq_activity_regs_student_term_active" in msg_lower:
                if is_matched:
                    raise HTTPException(
                        status_code=400,
                        detail="此學生本學期已有有效報名，請使用修改功能",
                    )
                return _silent_success_response
            raise
        _invalidate_after_registration_mutation(session)
        logger.info(
            "新報名提交：id=%s matched=%s",
            reg.id,
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
            # 明文 token 只在這次 response 回；DB 只存 hash，後續再也拿不到
            "query_token": plaintext_token,
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


@router.post("/public/update", response_model=PublicRegistrationDetailOut)
def public_update_registration(
    body: PublicUpdatePayload,
    request: Request,
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

    樂觀鎖（if_unmodified_since）：選填字段，由 /public/query 拿到的 updated_at 字串。
    若提供且與 reg.updated_at 不符（家長打開舊頁、校方已調整資料）→ 409 STALE，
    避免家長覆寫校方修改。沒帶 token 沿用舊行為（向後相容）。

    回傳：與 /public/query 同 schema 的完整 registration（含 field_state、
    新 updated_at），前端不需再呼叫一次 /public/query 取最新資料。
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
        # 通用錯誤：查不到 / 身分不符一律回相同訊息（資安 #5：有 token 報名強制 token）
        if not reg or not _parent_mutation_identity_ok(
            reg, body.name, body.birthday, body.parent_phone, body.query_token
        ):
            raise HTTPException(
                status_code=403,
                detail="查無對應報名，請確認三項資料是否與報名時一致",
            )

        # 樂觀鎖檢查：token 為不透明字串（前端原樣回拋），只做相等比較，
        # 不 parse datetime — 避免 TZ/microsecond precision 邊界問題。
        if body.if_unmodified_since is not None:
            current_token = reg.updated_at.isoformat() if reg.updated_at else None
            if current_token != body.if_unmodified_since:
                raise HTTPException(
                    status_code=409,
                    detail=("資料已被校方更新，請重新整理頁面確認最新狀態後再儲存。"),
                )

        # 為 audit / RegistrationChange 軌跡保留舊值（在任何寫入前快照）。
        # 課程/用品 diff 只比 name（不含 status）— 避免「候補升正式 / 重存」這類
        # status 轉態被誤讀成「家長退課再加課」。狀態變動由 RegistrationChange
        # description 補述，audit log 看 name 集合的進出即可。
        old_class_name = reg.class_name
        old_parent_phone = reg.parent_phone
        old_remark = reg.remark or ""
        old_course_names = sorted(
            n
            for (n,) in session.query(ActivityCourse.name)
            .join(RegistrationCourse, RegistrationCourse.course_id == ActivityCourse.id)
            .filter(RegistrationCourse.registration_id == reg.id)
            .all()
        )
        old_supply_names = sorted(
            n
            for (n,) in session.query(ActivitySupply.name)
            .join(RegistrationSupply, RegistrationSupply.supply_id == ActivitySupply.id)
            .filter(RegistrationSupply.registration_id == reg.id)
            .all()
        )

        # 匹配成功後的報名，班級由系統維護（Student.classroom），家長輸入班級僅供參考。
        # 與 /public/query 共用 _resolve_class_field_state，確保前端 class_editable=false 時
        # 後端必然會覆寫成系統班名，不會出現「前端鎖住但後端用家長輸入」的不一致。
        cls_state = _resolve_class_field_state(session, reg)
        if cls_state["real_classroom_name"]:
            classroom_name_to_store = cls_state["real_classroom_name"]
        else:
            # pending 或 classroom_id 已停用 → 允許家長透過更新修正
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

        # Finding K：不全刪重建 RegistrationCourse——保留未變更課程的 id/status。
        # 候補排序以 RegistrationCourse.id ASC 為準；全刪重建會把候補課程換成更大 id
        # 洗到隊尾（下次釋位升錯人），且 promoted_pending 會被靜默重建為 enrolled
        # 跳過 48h 確認窗。改 diff：刪移除的、留未變更的、稍後只 add 新增的。
        desired_course_ids = {courses_by_name[ci.name].id for ci in body.courses}
        existing_rc = (
            session.query(RegistrationCourse)
            .filter(RegistrationCourse.registration_id == reg.id)
            .all()
        )
        existing_course_ids = {rc.course_id for rc in existing_rc}
        for rc in existing_rc:
            if rc.course_id not in desired_course_ids:
                session.delete(rc)
        new_course_items = [
            ci
            for ci in body.courses
            if courses_by_name[ci.name].id not in existing_course_ids
        ]
        # Finding #16：用品比照課程改 diff，不再全刪重建。
        # 全刪重建會以「當前 DB 價」重新 price_snapshot，破壞報名當下的差異化保價，
        # 靜默改寫已報名應繳總額（用品調價後修改報名 → 已繳金額可能瞬間變成超繳觸 409）。
        # 改 diff：保留未變更用品原列與原 price_snapshot，只刪移除的、稍後只 add 新增的。
        desired_supply_ids = {supplies_by_name[si.name].id for si in body.supplies}
        existing_rs = (
            session.query(RegistrationSupply)
            .filter(RegistrationSupply.registration_id == reg.id)
            .all()
        )
        existing_supply_ids = {rs.supply_id for rs in existing_rs}
        for rs in existing_rs:
            if rs.supply_id not in desired_supply_ids:
                session.delete(rs)
        new_supply_items = [
            si
            for si in body.supplies
            if supplies_by_name[si.name].id not in existing_supply_ids
        ]
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
                # F-029：原訊息「此手機號碼已被其他報名使用」會形成 phone enumeration
                # oracle，攻擊者可枚舉任意 09 開頭手機是否在系統內出現過。
                # 資安 P1 (2026-05-07)：再進一步收緊
                # - 409 → 400（與 Pydantic 驗證失敗同 status code，攻擊者無法用
                #   status code 區分「手機已存在」與「其他驗證錯誤」）
                # - 加入 200-500ms 隨機延遲（同 /public/query LOW-3 模式）壓低 timing oracle
                # rate limit 仍由 _public_register_limiter 控制 5/min/IP。
                time.sleep(random.uniform(0.2, 0.5))
                raise HTTPException(
                    status_code=400,
                    detail="此手機號碼無法使用，請聯繫校方協助處理",
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

        # K：只 attach 新增課程（未變更的已保留原列/ id，移除的已刪）。
        _attach_courses(
            session, reg.id, new_course_items, courses_by_name, upd_enrolled_map
        )
        # #16：只 attach 新增用品（未變更的已保留原列與原 price_snapshot，移除的已刪）。
        _attach_supplies(session, reg.id, new_supply_items, supplies_by_name)

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

        # 顯式 bump updated_at：SQLAlchemy onupdate 只有 row 真有 dirty 欄位才觸發；
        # 家長若只改課程不改其他欄位，updated_at 不會自動推進，舊 token 還能再用一次。
        # 強制設值是樂觀鎖正確性的兜底（必須在 commit 前）。
        reg.updated_at = now_taipei_naive()
        # 組裝新舊 diff（兩層稽核 — AuditMiddleware 系統層 + RegistrationChange 業務層）
        new_course_names = sorted(
            n
            for (n,) in session.query(ActivityCourse.name)
            .join(RegistrationCourse, RegistrationCourse.course_id == ActivityCourse.id)
            .filter(RegistrationCourse.registration_id == reg.id)
            .all()
        )
        new_supply_names = sorted(
            n
            for (n,) in session.query(ActivitySupply.name)
            .join(RegistrationSupply, RegistrationSupply.supply_id == ActivitySupply.id)
            .filter(RegistrationSupply.registration_id == reg.id)
            .all()
        )

        diff: dict = {}
        if old_class_name != reg.class_name:
            diff["class_name"] = {"old": old_class_name, "new": reg.class_name}
        if old_parent_phone != reg.parent_phone:
            # 隱私：手機號只記「有更動」，不在 audit 還原舊號全文，避免落 audit 表後變成洩漏點
            diff["parent_phone_changed"] = True
        if old_remark != (reg.remark or ""):
            diff["remark"] = {"old": old_remark, "new": reg.remark or ""}
        if old_course_names != new_course_names:
            diff["courses"] = {"old": old_course_names, "new": new_course_names}
        if old_supply_names != new_supply_names:
            diff["supplies"] = {"old": old_supply_names, "new": new_supply_names}

        # AuditMiddleware 系統層稽核：透過 request.state 帶出 entity_id 與 changes
        request.state.audit_entity_id = str(reg.id)
        request.state.audit_changes = diff

        # RegistrationChange 業務層稽核：後台「異動紀錄」分頁需用此來源（與既有寫入點同層）
        if diff:
            change_summary_parts = []
            if "class_name" in diff:
                change_summary_parts.append(
                    f"班級：{diff['class_name']['old']} → {diff['class_name']['new']}"
                )
            if "courses" in diff:
                change_summary_parts.append("課程異動")
            if "supplies" in diff:
                change_summary_parts.append("用品異動")
            if "parent_phone_changed" in diff:
                change_summary_parts.append("家長電話異動")
            if "remark" in diff:
                change_summary_parts.append("備註異動")
            activity_service.log_change(
                session,
                reg.id,
                reg.student_name,
                "家長公開頁修改",
                "；".join(change_summary_parts) or "（無欄位變更）",
                "家長（公開頁）",
            )

        # 在 commit 前 flush 一次，讓 builder 看到最新 RegistrationCourse/Supply
        session.flush()
        response_payload = _build_public_query_payload(session, reg)
        response_payload["message"] = "資料更新成功！"

        session.commit()
        _invalidate_activity_dashboard_caches(session)
        logger.info("前台更新報名：id=%s", reg.id)
        return response_payload
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logger.error("前台更新報名失敗：%s", e)
        raise_safe_500(e)
    finally:
        session.close()


_public_confirm_limiter_instance = create_limiter(
    max_calls=10,
    window_seconds=60,
    name="activity_public_confirm",
    error_detail="操作過於頻繁，請稍後再試",
)
_public_confirm_limiter = _public_confirm_limiter_instance.as_dependency()


def _parent_mutation_identity_ok(
    reg, name: str, birthday: str, parent_phone: str, query_token: str | None
) -> bool:
    """公開破壞性 mutation 的身分驗證（資安 #5，Option A）。

    - 有 query_token_hash 的報名（新報名）：強制有效未過期 query_token + phone。
      PII 三欄（姓名+生日+手機）不再足夠 → 閉合「知道三欄即可破壞性操作」漏洞。
    - 無 token 的舊報名：沿用三欄（向後相容，因無 token 可驗）。

    回傳 bool；caller 自行 raise 統一錯誤碼（不洩漏是哪一項不符）。
    """
    phone_ok = _normalize_phone(reg.parent_phone) == _normalize_phone(parent_phone)
    if reg.query_token_hash is not None:
        if not query_token:
            return False
        if _hash_query_token(query_token) != reg.query_token_hash:
            return False
        if is_query_token_expired(reg.query_token_issued_at):
            return False
        return phone_ok
    return reg.student_name == name and reg.birthday == birthday and phone_ok


def _verify_parent_identity(
    session,
    registration_id: int,
    name: str,
    birthday: str,
    parent_phone: str,
    query_token: str | None = None,
) -> ActivityRegistration:
    """破壞性 mutation 身分驗證：有 token 報名強制 token，舊報名沿用三欄（資安 #5）。

    不符一律回 404 且不洩漏是哪一項錯，維持隱私契約。
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
    if not _parent_mutation_identity_ok(reg, name, birthday, parent_phone, query_token):
        raise HTTPException(status_code=404, detail="查無對應報名資料")
    return reg


class _PromotionActionPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    birthday: str = Field(..., min_length=1, max_length=20)
    parent_phone: str = Field(..., min_length=1, max_length=30)
    # 資安 #5：有 token 的報名強制帶有效未過期 query_token；舊報名沿用三欄。
    query_token: str | None = Field(None, max_length=256)


@router.post(
    "/public/registrations/{registration_id}/courses/{course_id}/confirm-promotion",
    response_model=DeleteResultOut,
)
def public_confirm_promotion(
    registration_id: int,
    course_id: int,
    body: _PromotionActionPayload,
    request: Request,
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
            session,
            registration_id,
            body.name,
            body.birthday,
            body.parent_phone,
            body.query_token,
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
        _invalidate_after_registration_mutation(session)
        # 家長端候補轉正影響名額/收費；AuditMiddleware 未涵蓋此 public 子路由，
        # 顯式留稽核（含 IP），對齊 /public/update 的軌跡可一起篩。
        write_explicit_audit(
            request,
            action="UPDATE",
            entity_type="activity_registration",
            entity_id=str(registration_id),
            summary=f"家長確認候補轉正：「{course_name}」（{student_name}）",
            changes={
                "course_id": course_id,
                "course_name": course_name,
                "student_name": student_name,
                "actor": "parent",
                "event": "confirm_promotion",
            },
        )
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
    "/public/registrations/{registration_id}/courses/{course_id}/decline-promotion",
    response_model=DeleteResultOut,
)
def public_decline_promotion(
    registration_id: int,
    course_id: int,
    body: _PromotionActionPayload,
    request: Request,
    _: None = Depends(_public_confirm_limiter),
):
    """家長放棄候補轉正（三欄驗證）。該課程報名會被刪除，遞補下一位。"""
    session = get_session()
    try:
        _verify_parent_identity(
            session,
            registration_id,
            body.name,
            body.birthday,
            body.parent_phone,
            body.query_token,
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
        _invalidate_after_registration_mutation(session)
        # 放棄會刪除該課程報名、釋出名額給下一位候補，更需留軌跡（含 IP）。
        write_explicit_audit(
            request,
            action="DELETE",
            entity_type="activity_registration",
            entity_id=str(registration_id),
            summary=f"家長放棄候補轉正（釋出名額）：「{course_name}」（{student_name}）",
            changes={
                "course_id": course_id,
                "course_name": course_name,
                "student_name": student_name,
                "actor": "parent",
                "event": "decline_promotion",
            },
        )
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


@router.post("/public/inquiries", status_code=201, response_model=DeleteResultOut)
def public_create_inquiry(
    body: PublicInquiryPayload,
    _: None = Depends(_public_inquiry_limiter),
):
    """前台：提交家長提問

    LOW-4：honeypot + 時序檢查若命中 → silent reject（回偽裝成功訊息、不寫 DB）。
    """
    if should_silent_reject_bot(body.hp, body.ts):
        logger.warning(
            "public_create_inquiry silent-reject (honeypot/ts)",
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
