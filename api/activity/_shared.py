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

# F2-aux：金額 / 字數 / 天數常數抽到 utils/activity_constants.py 單一來源
from utils.activity_constants import (  # noqa: E402
    MAX_PAYMENT_AMOUNT,
    MIN_REFUND_REASON_LENGTH,
    MIN_VOID_REASON_LENGTH,
    PAYMENT_DATE_BACK_LIMIT_DAYS,
)

# 系統補齊標記：用於 batch/update_payment 與退課自動沖帳。
# 目的：避免把「系統自動生成的繳/退費紀錄」誤算入 POS 日結的「現金」欄。
SYSTEM_RECONCILE_METHOD = "系統補齊"

# F2 第五階段：5 個金流簽核守衛抽到 services/activity_payment_guards.py。
# 閾值常數同步抽到 utils/activity_constants.py，本檔 re-export 維持 callers 不需動。
from utils.activity_constants import (  # noqa: E402, F401
    ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD,
    REFUND_APPROVAL_THRESHOLD,
)
from services.activity_payment_guards import (  # noqa: E402, F401
    has_payment_approve,
    require_refund_reason,
    require_approve_for_high_price,
    require_approve_for_large_refund,
    require_approve_for_cumulative_refund,
)

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

# F2 第七階段：學生主檔同步 helper 抽到 services/activity_student_sync.py。
# 本檔保留 re-export 維持 students.py / public.py / registrations.py 既有 import surface。
from services.activity_student_sync import (  # noqa: F401, E402
    _match_student_id,
    _match_student_with_parent_phone,
    sync_registrations_on_student_transfer,
    sync_registrations_on_student_deactivate,
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


# F2 第六階段：query token helper 抽到 services/activity_query_token.py。
# 本檔 re-export 維持既有 import surface（api/activity/public.py / registrations.py
# 等模組仍可 `from api.activity._shared import _hash_query_token` 取得）。
from services.activity_query_token import (  # noqa: E402, F401
    _query_token_ttl_days,
    is_query_token_expired,
    _generate_query_token,
    _hash_query_token,
)


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
