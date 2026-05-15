"""
api/activity/_shared.py — 才藝系統共用 schemas、helpers、常數
"""

import hashlib
import hmac
import json
import os
import re
import logging
import secrets as _secrets_module
from collections import defaultdict
from datetime import datetime, date, time, timedelta
from typing import Optional, List, Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request, Response
from fastapi.responses import Response as PlainResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select as sa_select
from sqlalchemy.exc import CompileError, OperationalError

from models.database import (
    get_session,
    Classroom,
    ActivityCourse,
    ActivitySupply,
    ActivityRegistration,
    RegistrationCourse,
    RegistrationSupply,
    ActivityPaymentRecord,
    ActivityPosDailyClose,
    ActivityRegistrationSettings,
    ActivitySession,
    ActivityAttendance,
)
from services.activity_service import activity_service
from services.report_cache_service import report_cache_service
from utils.auth import require_staff_permission, JWT_SECRET_KEY
from utils.permissions import Permission

logger = logging.getLogger(__name__)


from utils.finance_cache import (
    invalidate_finance_summary_cache as _invalidate_finance_summary_cache,
)


def _lock_registration(session, registration_id: int):
    """對單筆 registration 取得行級鎖；SQLite（單元測試）自動降級為無鎖。"""
    query = session.query(ActivityRegistration).filter(
        ActivityRegistration.id == registration_id,
        ActivityRegistration.is_active.is_(True),
    )
    try:
        return query.with_for_update().first()
    except (CompileError, OperationalError, NotImplementedError):
        return query.first()


TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 金額上限統一常數（同步 pos.py 的 _MAX_ITEM_AMOUNT）
MAX_PAYMENT_AMOUNT = 999_999

# 系統補齊標記：用於 batch/update_payment 與退課自動沖帳。
# 目的：避免把「系統自動生成的繳/退費紀錄」誤算入 POS 日結的「現金」欄。
SYSTEM_RECONCILE_METHOD = "系統補齊"

# payment_date 合理範圍：最多回補 30 天、不得指定未來。
# POS checkout 與後台 /registrations/{id}/payments 共用，避免管理員透過後者繞過 POS 管制。
PAYMENT_DATE_BACK_LIMIT_DAYS = 30

# 退費必填原因最短字數（避免「客人退」等敷衍；15 字強迫填寫具體事由）
MIN_REFUND_REASON_LENGTH = 15

# 退費金額閾值：超過此金額的單筆退費必須具備 ACTIVITY_PAYMENT_APPROVE 權限
# Why: 小額退費允許一線櫃檯彈性處理；大額退費強制雙簽以防內部舞弊
REFUND_APPROVAL_THRESHOLD = 1000

# 課程/用品單品價格高額閾值：超過此金額的設定/異動必須具備 ACTIVITY_PAYMENT_APPROVE。
# Why: 課程價格會被寫入 price_snapshot 進入應繳總額，搭配「補齊收入」路徑可建立異常高額
# 應收。一般幼稚園單品價格遠低於 30,000，超過視為設定錯誤或舞弊嘗試。
ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD = 30_000

# 軟刪除 payment 原因最短字數
MIN_VOID_REASON_LENGTH = 5


def has_payment_approve(current_user: dict) -> bool:
    """檢查使用者是否具備 ACTIVITY_PAYMENT_APPROVE 權限（老闆/高階簽核）。

    用於：大額退費審批、DELETE payment 軟刪審批。避免只有 ACTIVITY_WRITE 的一線員工
    直接執行敏感金流動作。
    """
    from utils.permissions import has_permission

    perms = current_user.get("permissions", 0)
    return has_permission(perms, Permission.ACTIVITY_PAYMENT_APPROVE)


def require_refund_reason(notes: Optional[str]) -> str:
    """驗證退費 notes（原因）必填且 ≥ MIN_REFUND_REASON_LENGTH 字。

    供 POS refund / add_registration_payment(type=refund) 共用。
    """
    cleaned = (notes or "").strip()
    if len(cleaned) < MIN_REFUND_REASON_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"退費必須填寫原因（至少 {MIN_REFUND_REASON_LENGTH} 個字）",
        )
    return cleaned


def require_approve_for_high_price(
    amount: int, current_user: dict, *, label: str = "單品價格"
) -> None:
    """若單品價格超過 ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD，檢查 ACTIVITY_PAYMENT_APPROVE。

    用於 Course/Supply create/update：避免 ACTIVITY_WRITE 一線權限可任意設定極端
    高價，搭配補齊收入路徑放大舞弊金額。
    """
    if amount > ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD and not has_payment_approve(
        current_user
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"{label} NT${amount:,} 超過 NT${ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD:,} 審批閾值，"
                f"需由具備『才藝課收款簽核』權限者執行"
            ),
        )


def require_approve_for_large_refund(
    amount: int, current_user: dict, *, label: str = "單筆退費金額"
) -> None:
    """若退費金額超過 REFUND_APPROVAL_THRESHOLD，檢查 ACTIVITY_PAYMENT_APPROVE 權限。

    `amount` 可為單筆金額或「累積後總額」；`label` 控制錯誤訊息語意，
    呼叫端傳累積值時請覆寫為「累積退費總額」等清楚字樣。
    不足即 403。供 POS refund / add_registration_payment(type=refund) 共用。
    """
    if amount > REFUND_APPROVAL_THRESHOLD and not has_payment_approve(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                f"{label} NT${amount} 超過 NT${REFUND_APPROVAL_THRESHOLD} 審批閾值，"
                f"需由具備『才藝課收款簽核』權限者執行"
            ),
        )


def require_approve_for_cumulative_refund(
    session,
    registration_id: int,
    this_refund_amount: int,
    current_user: dict,
    *,
    label: str,
) -> None:
    """以「該 reg 已存在 voided=NULL 的 refund 累積 + 本次」判斷是否跨閾值。

    Why: 與 add_registration_payment / pos.refund 既有累積判斷對齊。
    退課自動沖帳、刪除報名自動沖帳、標記未繳全額沖帳這三條 legacy 路徑只用
    「本次金額」過 require_approve_for_large_refund，可拆單跨閾值繞過簽核
    （reg 已退 NT$600 → 再退 NT$900 兩筆都 < NT$1000，但累積 NT$1500 應簽核）。

    Refs: 邏輯漏洞 audit 2026-05-07 P0 (#8)。
    """
    from sqlalchemy import func
    from models.database import ActivityPaymentRecord

    prior = (
        session.query(func.coalesce(func.sum(ActivityPaymentRecord.amount), 0))
        .filter(
            ActivityPaymentRecord.registration_id == registration_id,
            ActivityPaymentRecord.type == "refund",
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .scalar()
    ) or 0
    cumulative = int(prior) + int(this_refund_amount)
    require_approve_for_large_refund(cumulative, current_user, label=label)


# F2 第一階段：時區 helper 抽到 utils/taipei_time.py 共用（fees / activity / portal
# 都需要同一條台灣時區邏輯）。本檔保留 re-export 維持既有 import surface。
from utils.taipei_time import (  # noqa: F401
    TAIPEI_TZ as _TAIPEI_TZ_CANONICAL,  # 避免遮蔽本檔 line 62 既有變數
    now_taipei_naive,
    today_taipei,
)


def validate_payment_date(
    value: date, *, back_limit_days: int = PAYMENT_DATE_BACK_LIMIT_DAYS
) -> date:
    """驗證 payment_date 必須在今日回補窗內，不得指定未來。

    `back_limit_days` 預設 30（活動 POS 場景）；學費跨月分期需放寬，
    呼叫端可覆寫此參數。delegated to utils.taipei_time。
    """
    from utils.taipei_time import validate_payment_date as _impl

    return _impl(value, back_limit_days=back_limit_days)


# ── 服務注入 ──────────────────────────────────────────────────────────────

_line_service = None


def init_activity_services(line_svc) -> None:
    global _line_service
    _line_service = line_svc
    activity_service.set_line_service(line_svc)


def get_line_service():
    return _line_service


# ── 共用 HTTPException helpers ─────────────────────────────────────────────


def _not_found(resource: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"找不到{resource}")


def _duplicate_name(resource: str) -> HTTPException:
    return HTTPException(status_code=400, detail=f"{resource}名稱已存在")


def _invalid_class() -> HTTPException:
    return HTTPException(status_code=400, detail="班級不存在或已停用")


def _item_not_found_in_list(resource: str, name: str) -> HTTPException:
    return HTTPException(status_code=400, detail=f"找不到{resource}：{name}")


# ── Pydantic Schemas ────────────────────────────────────────────────────────


# F2 第三階段：Course / Supply CRUD schemas 抽到 schemas/activity_admin.py 共用。
# 本檔保留 re-export 維持 api/activity/courses.py / supplies.py 既有 import surface。
from schemas.activity_admin import (  # noqa: F401
    CourseCreate,
    CourseUpdate,
    SupplyCreate,
    SupplyUpdate,
    CopyCoursesRequest,
)

# F2 第四階段：剩餘 admin schemas（Payment / Void / Inquiry / Settings / Batch / Add）
# 抽到 schemas/activity_admin.py 共用。本檔保留 re-export 維持既有 import surface。
from schemas.activity_admin import (  # noqa: F401, E402
    PaymentUpdate,
    RemarkUpdate,
    VoidPaymentRequest,
    InquiryReply,
    RegistrationTimeSettings,
    BatchPaymentUpdate,
    AddPaymentRequest,
)

# F2 第二階段：公開報名 schemas + 驗證 helper 抽到 schemas/activity_public.py 共用。
# 本檔保留 re-export 維持既有 import surface（api/activity/public.py 等 6 模組不需動）。
from schemas.activity_public import (  # noqa: F401
    PublicCourseItem,
    PublicSupplyItem,
    PublicInquiryPayload,
    PublicRegistrationPayload,
    PublicUpdatePayload,
    should_silent_reject_bot,
    _validate_birthday_str,
    _normalize_phone,
    _validate_tw_mobile,
    _TW_MOBILE_RE,
)

# F2 第四階段：AdminRegistration* + AddCourse/Supply schemas 抽到 schemas/activity_admin.py。
from schemas.activity_admin import (  # noqa: F401, E402
    AdminRegistrationBasicUpdate,
    AddCourseRequest,
    AddSupplyRequest,
    AdminRegistrationPayload,
)

# ── DB 輔助函式 ─────────────────────────────────────────────────────────────


def _get_active_classroom(session, classroom_name: str):
    """依名稱取得啟用中的班級。"""
    return (
        session.query(Classroom)
        .filter(
            Classroom.name == classroom_name.strip(),
            Classroom.is_active.is_(True),
        )
        .first()
    )


def _require_active_classroom(session, classroom_name: str):
    """取得啟用中班級，不存在則拋 HTTPException(400)。"""
    c = _get_active_classroom(session, classroom_name)
    if not c:
        raise _invalid_class()
    return c


# ── Phase 3 公開查詢碼（query token） ──────────────────────────────────────
# 設計：
# - 明文 token = 32-char URL-safe（secrets.token_urlsafe(24)）
# - DB 只存 HMAC-SHA256(JWT_SECRET_KEY, domain || token) 的 hex digest
# - 只在 register response 一次性回明文 token 給家長；後續再也拿不到
# - reject pending 時 rotate（指向新 hash）
# - threat model：token 是 convenience layer，不是 security layer；phone 仍是必要第二因素
# - server secret 沿用 JWT_SECRET_KEY（dev 重啟會 invalidate 所有 token，已有 warning）
# - 到期：query_token_issued_at + ACTIVITY_QUERY_TOKEN_TTL_DAYS（預設 180 天）。
#   過期/未發 token 一律拒絕並引導改用 /public/query 三欄比對。Refs: 資安掃描 2026-05-07 P0。
_ACTIVITY_TOKEN_DOMAIN = b"activity_query_token:v1"


def _query_token_ttl_days() -> int:
    """讀環境變數 ACTIVITY_QUERY_TOKEN_TTL_DAYS（預設 180 天）。

    180 天涵蓋一個學期完整活動期 + 部分緩衝；業主可調為更短（例 90）強化。
    invalid 值 fallback 預設值，不 raise（避免一個壞 env 卡住整個公開報名頁）。
    """
    raw = os.getenv("ACTIVITY_QUERY_TOKEN_TTL_DAYS", "180")
    try:
        v = int(raw)
        return v if v > 0 else 180
    except (TypeError, ValueError):
        return 180


def is_query_token_expired(issued_at) -> bool:
    """判斷查詢碼是否已過期。

    issued_at 為 None（舊資料未發 token / backfill 期）一律視為過期。
    這樣攻擊者拿到舊 reg 的偽造 token 也無法用，必須走 /public/query 三欄比對。
    """
    if issued_at is None:
        return True
    ttl = timedelta(days=_query_token_ttl_days())
    return datetime.now() - issued_at > ttl


def _generate_query_token() -> str:
    """產生公開查詢碼明文（32-char URL-safe）。

    僅在 register 真實成功 / reject rotate 當下回給呼叫端。
    silent-success path 用同函式產一個「假」token（不寫 DB），維持 response shape
    一致避免 F-030 enumeration oracle。
    """
    return _secrets_module.token_urlsafe(24)


def _hash_query_token(token: str) -> str:
    """HMAC-SHA256(JWT_SECRET_KEY, domain || token) → hex digest（64 chars）。

    domain salt（_ACTIVITY_TOKEN_DOMAIN）做用途隔離 — 即使 JWT_SECRET_KEY 被
    其他模組借用，產生的 hash 不會撞號。
    """
    msg = _ACTIVITY_TOKEN_DOMAIN + token.encode("utf-8")
    key = (JWT_SECRET_KEY or "").encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _build_public_query_payload(session, reg) -> dict:
    """組裝 /public/query 與 /public/update 共用的 response payload。

    Why: /public/update 修改後若再讓前端打一次 /public/query 取最新資料，
    會多一次 round-trip + 中間又被改的 race window。改成 update 端點在 commit 前
    用同一個 helper 直接組 response，前端只要 hydrate 一次。
    隱私契約沿用 query 版本：不洩漏 student_id / classroom_id / match_status raw 值。

    回傳 dict 含 updated_at（ISO string，不透明字串），供前端作為樂觀鎖 token
    回傳給 /public/update 的 if_unmodified_since。
    """
    rc_rows = (
        session.query(RegistrationCourse, ActivityCourse)
        .join(ActivityCourse, RegistrationCourse.course_id == ActivityCourse.id)
        .filter(RegistrationCourse.registration_id == reg.id)
        .all()
    )

    # 一次查出所有候補課程的排位（window function，避免 N+1）
    waitlist_course_ids = [ac.id for rc, ac in rc_rows if rc.status == "waitlist"]
    waitlist_position_map: dict = {}
    waitlist_total_map: dict = {}
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
        waitlist_position_map = {row.course_id: row.position for row in waitlist_rows}

        # 每個候補課程的總候補人數（promoted_pending 不計入，只算 waitlist）
        total_rows = (
            session.query(
                RegistrationCourse.course_id,
                func.count(RegistrationCourse.id).label("total"),
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
            .group_by(RegistrationCourse.course_id)
            .all()
        )
        waitlist_total_map = {row.course_id: row.total for row in total_rows}

    courses = []
    for rc, ac in rc_rows:
        waitlist_position = None
        waitlist_total = None
        if rc.status == "waitlist":
            waitlist_position = waitlist_position_map.get(ac.id)
            waitlist_total = waitlist_total_map.get(ac.id)
        courses.append(
            {
                "name": ac.name,
                "course_id": ac.id,
                "price": rc.price_snapshot,
                "status": rc.status,
                "waitlist_position": waitlist_position,
                "waitlist_total": waitlist_total,
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

    total_amount = sum(c["price"] for c in courses if c["status"] == "enrolled")
    total_amount += sum(rs.price_snapshot for rs, sp in rs_rows)
    paid_amount = reg.paid_amount or 0

    cls_state = _resolve_class_field_state(session, reg)
    field_state = {
        "class_source": cls_state["class_source"],
        "class_editable": cls_state["class_editable"],
        "review_state": cls_state["review_state"],
    }

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
        "field_state": field_state,
        # 樂觀鎖 token：前端持有，回傳給 /public/update 的 if_unmodified_since。
        # 後端原樣字串比較，不 parse；/public/update 結尾顯式 bump 確保 row 一定 dirty。
        "updated_at": reg.updated_at.isoformat() if reg.updated_at else None,
    }


def _resolve_class_field_state(session, reg) -> dict:
    """為公開頁回傳「班級欄位 UI 狀態」與真實班級名（若已比對）。

    供 /public/query 與 /public/update 共用，確保前端鎖定條件
    與後端覆寫條件完全一致；避免「前端顯示可改 → 後端覆寫成系統班級」的
    UX 不一致。

    隱私契約：回傳值僅含 UI 用 hint（class_source/class_editable/review_state），
    絕不外洩 student_id / classroom_id / match_status raw 值。

    回傳 dict：
    - class_source: 'student_record'（已比對且班級啟用）| 'submitted'（家長自填）
    - class_editable: 是否可由家長修改（pending 才可編，已比對為唯讀）
    - review_state: 'confirmed'（已比對）| 'school_review'（待校方審核）
    - real_classroom_name: 已比對且班級啟用時的真實班名，否則 None
    """
    if reg.classroom_id:
        real = (
            session.query(Classroom)
            .filter(
                Classroom.id == reg.classroom_id,
                Classroom.is_active.is_(True),
            )
            .first()
        )
        if real:
            return {
                "class_source": "student_record",
                "class_editable": False,
                "review_state": "confirmed",
                "real_classroom_name": real.name,
            }
    return {
        "class_source": "submitted",
        "class_editable": True,
        "review_state": "school_review",
        "real_classroom_name": None,
    }


def _invalidate_activity_dashboard_caches(
    session, *, summary_only: bool = False
) -> None:
    if summary_only:
        activity_service.invalidate_summary_cache(session)
        return
    activity_service.invalidate_dashboard_caches(session)


def _public_etag_response(request: Request, response: Response, payload):
    """為公開端點套上 ETag + no-cache 行為。

    Why: 原本 `Cache-Control: max-age=300` 讓後台新增/異動課程後，前台需等
    5~6 分鐘才看到（CDN/瀏覽器都會吃住快取）。改成 ETag + no-cache：
    - no-cache 表示每次都需 revalidate，不會盲信舊快取
    - ETag 命中時回 304 不傳 body，伺服器負擔仍接近零

    payload 可為 dict / list / 任意可 JSON 序列化結構；命中 304 時回傳
    PlainResponse(304)，否則回 payload 並設好 ETag。
    """
    etag = (
        '"'
        + hashlib.md5(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        + '"'
    )
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    if request.headers.get("If-None-Match") == etag:
        return PlainResponse(status_code=304, headers={"ETag": etag})
    return payload


def _parse_settings_iso(value: Optional[str]) -> Optional[datetime]:
    """把 settings 存的 ISO 字串解析成 naive datetime（台灣時間語意）。

    相容三種歷史格式：`YYYY-MM-DDTHH:MM`、`YYYY-MM-DDTHH:MM:SS`、
    以及帶 `Z` 或 `+08:00` 的舊匯入資料；全部轉為 naive 台灣時間以便與 now 比較。
    無法解析時回 None（讓守衛視同未設定）。
    """
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(TAIPEI_TZ).replace(tzinfo=None)
    return dt


def _check_registration_open(session) -> None:
    """驗證報名時間是否開放，不符合時拋出 HTTPException。

    open_at / close_at 以 naive datetime 物件比對，避免字串格式差異（`Z` 尾綴、
    秒數有無等）造成比對錯判。
    """
    settings = session.query(ActivityRegistrationSettings).first()
    if not settings:
        return
    if not settings.is_open:
        raise HTTPException(status_code=400, detail="報名尚未開放")
    now = datetime.now(TAIPEI_TZ).replace(tzinfo=None)
    open_at = _parse_settings_iso(settings.open_at)
    close_at = _parse_settings_iso(settings.close_at)
    if open_at and now < open_at:
        raise HTTPException(status_code=400, detail="報名尚未開始")
    if close_at and now > close_at:
        raise HTTPException(status_code=400, detail="報名已截止")


def _attach_courses(
    session,
    reg_id: int,
    course_items,
    courses_by_name: dict,
    enrolled_count_map: dict,
) -> tuple[bool, list]:
    """建立 RegistrationCourse 記錄，回傳 (has_waitlist, waitlist_course_names)。

    enrolled_count_map 的值已包含 enrolled + promoted_pending（佔容量）。
    """
    has_waitlist = False
    waitlist_course_names: list = []
    for course_item in course_items:
        course = courses_by_name.get(course_item.name)
        if not course:
            raise _item_not_found_in_list("課程", course_item.name)
        occupying_count = enrolled_count_map.get(course.id, 0)
        capacity = course.capacity if course.capacity is not None else 30
        if occupying_count < capacity:
            status = "enrolled"
        elif course.allow_waitlist:
            status = "waitlist"
            has_waitlist = True
            waitlist_course_names.append(course.name)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"課程「{course.name}」已額滿且不開放候補",
            )
        rc = RegistrationCourse(
            registration_id=reg_id,
            course_id=course.id,
            status=status,
            price_snapshot=course.price,
        )
        session.add(rc)
    return has_waitlist, waitlist_course_names


def _attach_supplies(
    session, reg_id: int, supply_items, supplies_by_name: dict
) -> None:
    """建立 RegistrationSupply 記錄。"""
    for supply_item in supply_items:
        supply = supplies_by_name.get(supply_item.name)
        if not supply:
            raise _item_not_found_in_list("用品", supply_item.name)
        rs = RegistrationSupply(
            registration_id=reg_id,
            supply_id=supply.id,
            price_snapshot=supply.price,
        )
        session.add(rs)


def _calc_total_amount(session, registration_id: int) -> int:
    """計算報名的應繳總金額（enrolled 課程 + 用品，候補不計）"""
    course_total = (
        session.query(func.coalesce(func.sum(RegistrationCourse.price_snapshot), 0))
        .filter(
            RegistrationCourse.registration_id == registration_id,
            RegistrationCourse.status == "enrolled",
        )
        .scalar()
    ) or 0
    supply_total = (
        session.query(func.coalesce(func.sum(RegistrationSupply.price_snapshot), 0))
        .filter(RegistrationSupply.registration_id == registration_id)
        .scalar()
    ) or 0
    return course_total + supply_total


def _derive_payment_status(paid_amount: int, total_amount: int) -> str:
    """根據已繳金額衍生四態狀態字串（unpaid / partial / paid / overpaid）"""
    if total_amount == 0:
        return "paid" if paid_amount == 0 else "overpaid"
    if paid_amount > total_amount:
        return "overpaid"
    if paid_amount == total_amount:
        return "paid"
    if paid_amount > 0:
        return "partial"
    return "unpaid"


def _compute_is_paid(paid_amount: int, total_amount: int) -> bool:
    """統一 is_paid 計算：應繳為 0 的報名一律視為未結清，避免全免課程誤判為已繳。

    Why: 原先 10+ 處各自 inline 計算、有兩種等價寫法（`paid >= total > 0` vs
    `total > 0 and paid >= total`），易漂移。以 helper 集中。
    """
    return total_amount > 0 and paid_amount >= total_amount


def _is_daily_closed(session, target_date: date) -> bool:
    """判斷 target_date 是否已完成 POS 日結簽核。供 service / router 共用。"""
    if target_date is None:
        return False
    closed = (
        session.query(ActivityPosDailyClose.close_date)
        .filter(ActivityPosDailyClose.close_date == target_date)
        .first()
    )
    return closed is not None


def _require_daily_close_unlocked(session, target_date: date) -> None:
    """拒絕寫入 payment_date 落在已 daily-close 的紀錄。

    Why: payment_date 允許回補 30 天，但若該日已被老闆簽核，snapshot 已凍結。
    此時新增交易會讓 DB 實際值與 snapshot 永久失準（reconciliation 永遠用 snapshot）。
    """
    if _is_daily_closed(session, target_date):
        raise HTTPException(
            status_code=400,
            detail=(
                f"日期 {target_date.isoformat()} 已完成日結簽核，"
                f"無法再新增/修改該日交易。請先解鎖日結後再操作。"
            ),
        )


def compute_daily_snapshot(session, target_date: date) -> dict:
    """某日 POS 流水即時快照：payment_total / refund_total / net_total / transaction_count / by_method。

    供 POS daily-summary 端點與日結簽核共用，避免邏輯雙寫。
    by_method 為 dict：員工輸入只可能是「現金」（POS schema 收口）；
    系統內部沖帳會出現「系統補齊」；method 為 NULL 者歸類為「未指定」（歷史資料）。

    Voided 紀錄（軟刪）一律排除，避免讓老闆簽核的總額包含已被作廢的交易。
    """
    rows = (
        session.query(
            ActivityPaymentRecord.type,
            ActivityPaymentRecord.payment_method,
            func.count(ActivityPaymentRecord.id),
            func.coalesce(func.sum(ActivityPaymentRecord.amount), 0),
        )
        .filter(
            ActivityPaymentRecord.payment_date == target_date,
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .group_by(
            ActivityPaymentRecord.type,
            ActivityPaymentRecord.payment_method,
        )
        .all()
    )

    payment_total = 0
    refund_total = 0
    payment_count = 0
    refund_count = 0
    by_method_map: dict = defaultdict(lambda: {"payment": 0, "refund": 0, "count": 0})
    for rec_type, method, cnt, amt in rows:
        amt_int = int(amt or 0)
        cnt_int = int(cnt or 0)
        method_key = method or "未指定"
        if rec_type == "payment":
            payment_total += amt_int
            payment_count += cnt_int
            by_method_map[method_key]["payment"] += amt_int
        else:
            refund_total += amt_int
            refund_count += cnt_int
            by_method_map[method_key]["refund"] += amt_int
        by_method_map[method_key]["count"] += cnt_int

    by_method_list = [
        {
            "method": method_key,
            "payment": data["payment"],
            "refund": data["refund"],
            "count": data["count"],
        }
        for method_key, data in sorted(by_method_map.items())
    ]
    # by_method_net：簽核 snapshot 只需要淨額分佈，不需要 payment/refund 拆分
    by_method_net = {
        method_key: data["payment"] - data["refund"]
        for method_key, data in by_method_map.items()
    }

    return {
        "date": target_date.isoformat(),
        "payment_total": payment_total,
        "refund_total": refund_total,
        "net": payment_total - refund_total,
        "payment_count": payment_count,
        "refund_count": refund_count,
        "transaction_count": payment_count + refund_count,
        "by_method": by_method_list,
        "by_method_net": by_method_net,
    }


def _batch_calc_total_amounts(session, reg_ids: list) -> dict:
    """批次計算多筆報名的應繳總金額（2 次 GROUP BY，避免 N+1 查詢）"""
    course_totals = dict(
        session.query(
            RegistrationCourse.registration_id,
            func.coalesce(func.sum(RegistrationCourse.price_snapshot), 0),
        )
        .filter(
            RegistrationCourse.registration_id.in_(reg_ids),
            RegistrationCourse.status == "enrolled",
        )
        .group_by(RegistrationCourse.registration_id)
        .all()
    )
    supply_totals = dict(
        session.query(
            RegistrationSupply.registration_id,
            func.coalesce(func.sum(RegistrationSupply.price_snapshot), 0),
        )
        .filter(RegistrationSupply.registration_id.in_(reg_ids))
        .group_by(RegistrationSupply.registration_id)
        .all()
    )
    return {
        rid: (course_totals.get(rid, 0) or 0) + (supply_totals.get(rid, 0) or 0)
        for rid in reg_ids
    }


def _build_registration_filter_query(
    session,
    *,
    search: Optional[str] = None,
    payment_status: Optional[str] = None,
    course_id: Optional[int] = None,
    classroom_name: Optional[str] = None,
    school_year: Optional[int] = None,
    semester: Optional[int] = None,
    match_status: Optional[str] = None,
    include_inactive: bool = False,
    student_id: Optional[int] = None,
):
    """回傳已套用篩選條件的 SQLAlchemy query，調用方可繼續加 .offset/.limit 或 .all()。

    match_status：pending / matched / manual / rejected / unmatched，用於後台審核分頁。
    include_inactive：rejected 狀態的 registration 會被設為 is_active=False；若要列出
      rejected，需設 include_inactive=True。
    student_id：指定學生 ID 時，僅回傳該學生在校 Student.id 匹配的報名（含跨學期歷史）。
    """
    q = session.query(ActivityRegistration)
    if not include_inactive:
        q = q.filter(ActivityRegistration.is_active.is_(True))
    if student_id is not None:
        q = q.filter(ActivityRegistration.student_id == student_id)
    if school_year is not None:
        q = q.filter(ActivityRegistration.school_year == school_year)
    if semester is not None:
        q = q.filter(ActivityRegistration.semester == semester)
    if match_status:
        q = q.filter(ActivityRegistration.match_status == match_status)
    if search:
        like = f"%{search}%"
        q = q.filter(
            or_(
                ActivityRegistration.student_name.ilike(like),
                ActivityRegistration.class_name.ilike(like),
                ActivityRegistration.parent_phone.ilike(like),
            )
        )
    if payment_status == "paid":
        q = q.filter(ActivityRegistration.is_paid.is_(True))
    elif payment_status == "partial":
        q = q.filter(
            ActivityRegistration.paid_amount > 0,
            ActivityRegistration.is_paid.is_(False),
        )
    elif payment_status == "unpaid":
        q = q.filter(ActivityRegistration.paid_amount == 0)
    elif payment_status == "overpaid":
        course_total_sq = (
            sa_select(func.coalesce(func.sum(RegistrationCourse.price_snapshot), 0))
            .where(
                RegistrationCourse.registration_id == ActivityRegistration.id,
                RegistrationCourse.status == "enrolled",
            )
            .scalar_subquery()
        )
        supply_total_sq = (
            sa_select(func.coalesce(func.sum(RegistrationSupply.price_snapshot), 0))
            .where(RegistrationSupply.registration_id == ActivityRegistration.id)
            .scalar_subquery()
        )
        q = q.filter(
            ActivityRegistration.paid_amount > course_total_sq + supply_total_sq,
            ActivityRegistration.paid_amount > 0,
        )
    if course_id is not None:
        q = q.join(
            RegistrationCourse,
            RegistrationCourse.registration_id == ActivityRegistration.id,
        ).filter(RegistrationCourse.course_id == course_id)
    if classroom_name:
        q = q.filter(ActivityRegistration.class_name == classroom_name)
    return q


def _match_student_id(session, name: str, birthday: str) -> Optional[int]:
    """public 報名時以 (name, birthday) 嘗試匹配 students.id。

    同時匹配到多個學生則回 None（避免錯誤關聯）。
    """
    from models.database import Student
    from datetime import date as _date

    try:
        bday = _date.fromisoformat(birthday)
    except (ValueError, TypeError):
        return None

    q = session.query(Student.id).filter(
        Student.name == name.strip(),
        Student.birthday == bday,
        Student.is_active.is_(True),
    )
    rows = q.limit(2).all()
    if len(rows) == 1:
        return rows[0][0]
    return None


def _match_student_with_parent_phone(
    session, name: str, birthday: str, parent_phone: Optional[str]
) -> tuple[Optional[int], Optional[int]]:
    """三欄比對（姓名 + 生日 + 家長手機）取得在籍學生。

    - phone 同時與 Student.parent_phone、Student.emergency_contact_phone 比對
      （任一正規化後相符即匹配）
    - 先以 (name, birthday, is_active=True) 篩出候選（通常 0-2 筆），再
      Python 端正規化比對 phone，避開 SQL regex 全表掃描
    - 多筆匹配（歧義）→ 回 (None, None)，讓上游進入 pending_review
    - 無匹配 → 回 (None, None)
    - 成功 → 回 (student_id, classroom_id)
    """
    from models.database import Student
    from datetime import date as _date

    normalized_input = _normalize_phone(parent_phone)
    if not normalized_input:
        return (None, None)

    try:
        bday = _date.fromisoformat(birthday)
    except (ValueError, TypeError):
        return (None, None)

    candidates = (
        session.query(
            Student.id,
            Student.classroom_id,
            Student.parent_phone,
            Student.emergency_contact_phone,
        )
        .filter(
            Student.name == name.strip(),
            Student.birthday == bday,
            Student.is_active.is_(True),
        )
        .limit(10)
        .all()
    )

    matches: list[tuple[int, Optional[int]]] = []
    for sid, classroom_id, pp, ep in candidates:
        if (
            _normalize_phone(pp) == normalized_input
            or _normalize_phone(ep) == normalized_input
        ):
            matches.append((sid, classroom_id))

    if len(matches) == 1:
        return matches[0]
    return (None, None)


def sync_registrations_on_student_transfer(
    session, student_id: int, new_classroom_id: Optional[int]
) -> int:
    """學生轉班時，同步更新該生當前學期仍啟用的 ActivityRegistration 班級資訊。

    - 只處理 is_active=True 的報名（rejected / 軟刪除的不動）
    - 只處理當前學期（不回頭改歷史，歷史才藝名單應保持原樣）
    - classroom_id 改寫為 new_classroom_id；class_name 改為新班級的 Classroom.name（當前）
      若 new_classroom_id 為 None 或查不到班級，只更新 classroom_id，保留原 class_name 字串
    - 回傳更新筆數
    """
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()

    regs = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.student_id == student_id,
            ActivityRegistration.is_active.is_(True),
            ActivityRegistration.school_year == sy,
            ActivityRegistration.semester == sem,
        )
        .all()
    )
    if not regs:
        return 0

    new_classroom_name: Optional[str] = None
    if new_classroom_id is not None:
        new_classroom = (
            session.query(Classroom).filter(Classroom.id == new_classroom_id).first()
        )
        if new_classroom:
            new_classroom_name = new_classroom.name

    for r in regs:
        r.classroom_id = new_classroom_id
        if new_classroom_name:
            r.class_name = new_classroom_name

    return len(regs)


def sync_registrations_on_student_deactivate(
    session, student_id: int, *, current_user: Optional[dict] = None
) -> int:
    """學生畢業 / 退學 / 刪除時，軟刪該生當前學期啟用中 ActivityRegistration。

    - 把 is_active 設為 False；保留原 match_status（供後台稽核）
    - 只處理當前學期（歷史學期的報名維持原狀，仍可供報表追溯）
    - **若 paid_amount > 0**：自動寫一筆「系統補齊」退費沖帳紀錄並清零，
      並以 logger.warning 留痕提醒管理員處理實體退款；避免幽靈金額留存
    - **金流守衛**：若有任何 paid_amount > 0 且呼叫者未具 ACTIVITY_PAYMENT_APPROVE
      則 403。避免具 STUDENTS_WRITE/STUDENTS_LIFECYCLE_WRITE 但無金流簽核者
      透過學生狀態變更繞過活動退費端點的金流控管。
      呼叫端在 pure-system 場景（如背景任務、無 user 上下文）可省略 current_user，
      但生產 API handler 必須傳入；省略視為內部呼叫。
    - 回傳影響筆數
    """
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()

    regs = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.student_id == student_id,
            ActivityRegistration.is_active.is_(True),
            ActivityRegistration.school_year == sy,
            ActivityRegistration.semester == sem,
        )
        .all()
    )
    today = datetime.now(TAIPEI_TZ).date()
    has_paid = any((r.paid_amount or 0) > 0 for r in regs)
    # 若今日已簽核且有任何一筆需沖帳，直接拋以維持 snapshot 一致性
    # （上層 students.py 的刪除流程會因此回 400；需先解鎖日結再刪學生）
    if has_paid:
        _require_daily_close_unlocked(session, today)
        # 金流守衛：要求呼叫者具備 ACTIVITY_PAYMENT_APPROVE
        if current_user is not None and not has_payment_approve(current_user):
            paid_total = sum(r.paid_amount or 0 for r in regs)
            raise HTTPException(
                status_code=403,
                detail=(
                    f"該生有 {sum(1 for r in regs if (r.paid_amount or 0) > 0)} 筆"
                    f"已繳費才藝報名（合計 NT${paid_total:,}）。"
                    "離園/刪除學生會自動沖帳全額退費，需具備『才藝課收款簽核』權限"
                    "（ACTIVITY_PAYMENT_APPROVE）。請改由具該權限者執行，或先至活動退費端點"
                    "個別處理退款後再刪除學生。"
                ),
            )
    for r in regs:
        current_paid = r.paid_amount or 0
        if current_paid > 0:
            session.add(
                ActivityPaymentRecord(
                    registration_id=r.id,
                    type="refund",
                    amount=current_paid,
                    payment_date=today,
                    payment_method=SYSTEM_RECONCILE_METHOD,
                    notes="（學生離園同步軟刪自動沖帳）",
                    operator="system",
                )
            )
            r.paid_amount = 0
            r.is_paid = False
            logger.warning(
                "學生離園同步軟刪報名自動沖帳：reg_id=%s student_id=%s refunded=NT$%d，"
                "請管理員跟進實體退款",
                r.id,
                student_id,
                current_paid,
            )
            # 補 RegistrationChange 軌跡：前台 Dashboard「異動紀錄」才能看到這類被動退費事件
            activity_service.log_change(
                session,
                r.id,
                r.student_name,
                "學生離園自動沖帳",
                f"學生離園同步軟刪，系統寫退費紀錄 NT${current_paid}，請跟進實體退款",
                "system",
            )
        r.is_active = False
    return len(regs)


def _fetch_reg_course_names(session, reg_ids: list) -> dict:
    """批次查詢報名對應的課程名稱（含候補標記），回傳 {reg_id: [course_name, ...]}。"""
    course_name_map: dict = defaultdict(list)
    if reg_ids:
        course_rows = (
            session.query(
                RegistrationCourse.registration_id,
                RegistrationCourse.status,
                ActivityCourse.name,
            )
            .join(ActivityCourse, RegistrationCourse.course_id == ActivityCourse.id)
            .filter(RegistrationCourse.registration_id.in_(reg_ids))
            .all()
        )
        for registration_id, status, course_name in course_rows:
            course_name_map[registration_id].append(
                f"{course_name}（候補）" if status == "waitlist" else course_name
            )
    return course_name_map


def _fetch_reg_course_details(session, reg_ids: list) -> dict:
    """批次查詢報名對應的課程明細，回傳 {reg_id: [{name, price, status}, ...]}。

    與 _fetch_reg_course_names 不同：此函式保留 price_snapshot 與 status 欄位，
    供 POS 收據列印需要的完整明細。"""
    detail_map: dict = defaultdict(list)
    if reg_ids:
        rows = (
            session.query(
                RegistrationCourse.registration_id,
                RegistrationCourse.status,
                RegistrationCourse.price_snapshot,
                ActivityCourse.name,
            )
            .join(ActivityCourse, RegistrationCourse.course_id == ActivityCourse.id)
            .filter(RegistrationCourse.registration_id.in_(reg_ids))
            .all()
        )
        for registration_id, status, price_snapshot, course_name in rows:
            detail_map[registration_id].append(
                {
                    "name": course_name,
                    "price": price_snapshot or 0,
                    "status": status,
                }
            )
    return detail_map


def _fetch_reg_supplies(session, reg_ids: list) -> dict:
    """批次查詢報名對應的用品明細，回傳 {reg_id: [{name, price}, ...]}。"""
    supply_map: dict = defaultdict(list)
    if reg_ids:
        rows = (
            session.query(
                RegistrationSupply.registration_id,
                RegistrationSupply.price_snapshot,
                ActivitySupply.name,
            )
            .join(ActivitySupply, RegistrationSupply.supply_id == ActivitySupply.id)
            .filter(RegistrationSupply.registration_id.in_(reg_ids))
            .all()
        )
        for registration_id, price_snapshot, supply_name in rows:
            supply_map[registration_id].append(
                {
                    "name": supply_name,
                    "price": price_snapshot or 0,
                }
            )
    return supply_map


def _build_session_detail_response(
    db_session,
    sess: "ActivitySession",
    *,
    classroom_ids_filter: list | None = None,
    group_by: Optional[str] = None,
) -> dict:
    """取得場次詳情 + 出席狀態，供管理端及 Portal 共用。

    classroom_ids_filter=None  → 包含所有 enrolled 學生（管理端）
    classroom_ids_filter=[...] → 只包含指定班級的學生（Portal，FK 比對）
    group_by="classroom"       → 額外回傳 groups：按 classroom_id 分組

    篩選條件：
    - RegistrationCourse.status == 'enrolled'
    - ActivityRegistration.is_active == True
    - ActivityRegistration.match_status != 'rejected'（保險）
    - 若有 student_id：對應 Student.is_active == True（已畢業/退學學生不出現）
    """
    from models.database import Student

    course = (
        db_session.query(ActivityCourse)
        .filter(ActivityCourse.id == sess.course_id)
        .first()
    )

    enrolled_query = (
        db_session.query(
            RegistrationCourse.registration_id,
            ActivityRegistration.student_name,
            ActivityRegistration.class_name,
            ActivityRegistration.student_id,
            ActivityRegistration.classroom_id,
            Classroom.name.label("real_classroom_name"),
        )
        .join(
            ActivityRegistration,
            RegistrationCourse.registration_id == ActivityRegistration.id,
        )
        .outerjoin(
            Classroom,
            Classroom.id == ActivityRegistration.classroom_id,
        )
        .outerjoin(
            Student,
            Student.id == ActivityRegistration.student_id,
        )
        .filter(
            RegistrationCourse.course_id == sess.course_id,
            RegistrationCourse.status == "enrolled",
            ActivityRegistration.is_active.is_(True),
            ActivityRegistration.match_status != "rejected",
            # 若 student_id 為 None（校外生或 pending），或對應 Student 尚啟用，都保留
            or_(
                ActivityRegistration.student_id.is_(None),
                Student.is_active.is_(True),
            ),
        )
        .order_by(ActivityRegistration.class_name, ActivityRegistration.student_name)
    )
    if classroom_ids_filter is not None:
        if classroom_ids_filter:
            enrolled_query = enrolled_query.filter(
                ActivityRegistration.classroom_id.in_(classroom_ids_filter)
            )
        else:
            # 空列表表示沒有管轄班級，不應看到任何學生
            enrolled_query = enrolled_query.filter(False)
    enrolled = enrolled_query.all()

    reg_ids = [e.registration_id for e in enrolled]
    att_map: dict = {}
    if reg_ids:
        atts = (
            db_session.query(ActivityAttendance)
            .filter(
                ActivityAttendance.session_id == sess.id,
                ActivityAttendance.registration_id.in_(reg_ids),
            )
            .all()
        )
        att_map = {a.registration_id: a for a in atts}

    students = []
    for e in enrolled:
        att = att_map.get(e.registration_id)
        students.append(
            {
                "registration_id": e.registration_id,
                "student_id": e.student_id,
                "classroom_id": e.classroom_id,
                "student_name": e.student_name,
                # class_name：優先用真實班級（JOIN 到 Classroom），否則 fallback 到字串快照
                "class_name": e.real_classroom_name or e.class_name or "",
                "is_present": att.is_present if att is not None else None,
                "attendance_notes": att.notes or "" if att is not None else "",
            }
        )
    present_count = sum(1 for s in students if s["is_present"] is True)
    absent_count = sum(1 for s in students if s["is_present"] is False)

    response = {
        "id": sess.id,
        "course_id": sess.course_id,
        "course_name": course.name if course else "",
        "session_date": sess.session_date.isoformat(),
        "notes": sess.notes or "",
        "created_by": sess.created_by,
        "created_at": sess.created_at.isoformat() if sess.created_at else None,
        "students": students,
        "total": len(students),
        "present_count": present_count,
        "absent_count": absent_count,
    }

    if group_by == "classroom":
        # 按 classroom_id 分組；未分班（classroom_id=None）歸「未分班」末尾
        group_map: dict = {}
        for s in students:
            cid = s.get("classroom_id")
            cname = s.get("class_name") or "未分班"
            key = cid if cid is not None else "__unassigned__"
            if key not in group_map:
                group_map[key] = {
                    "classroom_id": cid,
                    "classroom_name": cname if cid is not None else "未分班",
                    "students": [],
                }
            group_map[key]["students"].append(s)
        # 未分班永遠排在最後
        classified = [g for k, g in group_map.items() if k != "__unassigned__"]
        classified.sort(key=lambda g: g["classroom_name"])
        unassigned = group_map.get("__unassigned__")
        if unassigned:
            classified.append(unassigned)
        response["groups"] = classified

    return response
