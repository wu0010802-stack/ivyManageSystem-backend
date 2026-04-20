"""
api/activity/_shared.py — 才藝系統共用 schemas、helpers、常數
"""

import re
import logging
from collections import defaultdict
from datetime import datetime, date
from typing import Optional, List, Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select as sa_select

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
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 金額上限統一常數（同步 pos.py 的 _MAX_ITEM_AMOUNT）
MAX_PAYMENT_AMOUNT = 999_999

# 系統補齊標記：用於 batch/update_payment 與退課自動沖帳。
# 目的：避免把「系統自動生成的繳/退費紀錄」誤算入 POS 日結的「現金」欄。
SYSTEM_RECONCILE_METHOD = "系統補齊"

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


class CourseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    price: int = Field(..., ge=0)
    sessions: Optional[int] = Field(None, ge=1)
    capacity: int = Field(30, ge=1)
    video_url: Optional[str] = None
    allow_waitlist: bool = True
    description: Optional[str] = None
    # 學期（不指定時 API 端會用當前學期填入）
    school_year: Optional[int] = Field(None, ge=100, le=200)
    semester: Optional[int] = Field(None, ge=1, le=2)


class CourseUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    price: Optional[int] = Field(None, ge=0)
    sessions: Optional[int] = Field(None, ge=1)
    capacity: Optional[int] = Field(None, ge=1)
    video_url: Optional[str] = None
    allow_waitlist: Optional[bool] = None
    description: Optional[str] = None


class SupplyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    price: int = Field(..., ge=0)
    school_year: Optional[int] = Field(None, ge=100, le=200)
    semester: Optional[int] = Field(None, ge=1, le=2)


class SupplyUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    price: Optional[int] = Field(None, ge=0)


class CopyCoursesRequest(BaseModel):
    """一鍵複製上學期課程到新學期的請求。"""

    source_school_year: int = Field(..., ge=100, le=200)
    source_semester: int = Field(..., ge=1, le=2)
    target_school_year: int = Field(..., ge=100, le=200)
    target_semester: int = Field(..., ge=1, le=2)


class PaymentUpdate(BaseModel):
    is_paid: bool


class RemarkUpdate(BaseModel):
    remark: str


class InquiryReply(BaseModel):
    reply: str = Field(..., min_length=1, max_length=2000)


class RegistrationTimeSettings(BaseModel):
    is_open: bool
    open_at: Optional[str] = None
    close_at: Optional[str] = None
    # 前台顯示客製化（全部可選；為 None 時前端 fallback 至預設）
    page_title: Optional[str] = Field(None, max_length=200)
    term_label: Optional[str] = Field(None, max_length=50)
    event_date_label: Optional[str] = Field(None, max_length=50)
    target_audience: Optional[str] = Field(None, max_length=100)
    form_card_title: Optional[str] = Field(None, max_length=200)
    poster_url: Optional[str] = Field(None, max_length=500)

    @field_validator("open_at", "close_at")
    @classmethod
    def validate_iso_format(cls, v):
        if v is not None:
            pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$"
            if not re.match(pattern, v):
                raise ValueError("時間格式必須為 ISO 8601（YYYY-MM-DDTHH:MM）")
        return v

    @model_validator(mode="after")
    def validate_close_after_open(self):
        if self.open_at and self.close_at and self.close_at <= self.open_at:
            raise ValueError("close_at 必須晚於 open_at")
        return self


class BatchPaymentUpdate(BaseModel):
    ids: List[int] = Field(..., min_length=1, max_length=500)
    is_paid: bool


class AddPaymentRequest(BaseModel):
    type: Literal["payment", "refund"] = "payment"
    amount: int = Field(
        ...,
        gt=0,
        le=MAX_PAYMENT_AMOUNT,
        description=f"金額（正整數，上限 NT${MAX_PAYMENT_AMOUNT:,}；type 決定方向）",
    )
    payment_date: date
    payment_method: Literal["現金", "轉帳", "其他"] = "現金"
    notes: str = Field("", max_length=200)
    idempotency_key: Optional[str] = Field(
        None,
        description="冪等 key（8-64 英數/底線/連字號）；同 key 在 10 分鐘內視為重試並回傳先前結果",
    )

    @field_validator("idempotency_key")
    @classmethod
    def _validate_idk(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not re.match(r"^[A-Za-z0-9_-]{8,64}$", v):
            raise ValueError("idempotency_key 格式不合（需 8-64 英數/底線/連字號）")
        return v


class PublicCourseItem(BaseModel):
    name: str
    price: str  # 相容保留，後端實際以 DB 價格為準


class PublicSupplyItem(BaseModel):
    name: str
    price: str  # 相容保留，後端實際以 DB 價格為準


class PublicInquiryPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    phone: str = Field(..., min_length=1, max_length=30)
    question: str = Field(..., min_length=1, max_length=2000)


_TW_MOBILE_RE = re.compile(r"^09\d{8}$")


def _normalize_phone(raw: Optional[str]) -> Optional[str]:
    """台灣手機正規化：去除空白/連字號/括號/點，保留數字；空字串回 None。"""
    if raw is None:
        return None
    digits = re.sub(r"[\s\-().]", "", str(raw))
    return digits or None


def _validate_tw_mobile(raw: Optional[str]) -> str:
    digits = _normalize_phone(raw)
    if not digits or not _TW_MOBILE_RE.match(digits):
        raise ValueError("家長手機格式錯誤（請輸入 09 開頭 10 碼）")
    return digits


class PublicRegistrationPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=50)
    birthday: str
    class_: str = Field(..., min_length=1, alias="class")
    parent_phone: str = Field(..., min_length=8, max_length=30)
    courses: list[PublicCourseItem] = Field(..., max_length=20)
    supplies: list[PublicSupplyItem] = Field(default=[], max_length=20)
    remark: str = Field(default="", max_length=500)
    # 前端可選擇性傳入；不傳時 API 端用當前學期
    school_year: Optional[int] = Field(None, ge=100, le=200)
    semester: Optional[int] = Field(None, ge=1, le=2)

    @field_validator("birthday")
    @classmethod
    def validate_birthday(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("生日格式必須為 YYYY-MM-DD")
        return v

    @field_validator("name", "class_", mode="before")
    @classmethod
    def strip_whitespace(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("parent_phone", mode="before")
    @classmethod
    def normalize_parent_phone(cls, v):
        return _validate_tw_mobile(v)


class PublicUpdatePayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int
    name: str = Field(..., min_length=1, max_length=50)
    birthday: str
    class_: str = Field(..., min_length=1, alias="class")
    parent_phone: str = Field(..., min_length=8, max_length=30)
    # 選填：家長換號碼。提供時以 parent_phone（舊號）做身份驗證，
    # 通過後 reg.parent_phone 改為 new_parent_phone。
    new_parent_phone: Optional[str] = Field(None, min_length=8, max_length=30)
    courses: list[PublicCourseItem] = Field(..., max_length=20)
    supplies: list[PublicSupplyItem] = Field(default=[], max_length=20)
    remark: str = ""

    @field_validator("birthday")
    @classmethod
    def validate_birthday(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("生日格式必須為 YYYY-MM-DD")
        return v

    @field_validator("name", "class_", mode="before")
    @classmethod
    def strip_whitespace(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("parent_phone", mode="before")
    @classmethod
    def normalize_parent_phone(cls, v):
        return _validate_tw_mobile(v)

    @field_validator("new_parent_phone", mode="before")
    @classmethod
    def normalize_new_parent_phone(cls, v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return _validate_tw_mobile(v)


class AdminRegistrationBasicUpdate(BaseModel):
    """後台編輯報名基本欄位（不含課程/用品/備註）。"""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=50)
    birthday: str
    class_: str = Field(..., min_length=1, alias="class")
    email: Optional[str] = Field(None, max_length=200)

    @field_validator("birthday")
    @classmethod
    def validate_birthday(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("生日格式必須為 YYYY-MM-DD")
        return v

    @field_validator("name", "class_", mode="before")
    @classmethod
    def strip_whitespace(cls, v):
        return v.strip() if isinstance(v, str) else v


class AddCourseRequest(BaseModel):
    """後台為既有報名新增一筆課程。"""

    course_id: int = Field(..., gt=0)


class AddSupplyRequest(BaseModel):
    """後台為既有報名新增一筆用品。"""

    supply_id: int = Field(..., gt=0)


class AdminRegistrationPayload(BaseModel):
    """後台手動新增報名的 payload。

    與 PublicRegistrationPayload 差異：
    - 不強制檢查報名開放時間（後台可隨時建立）
    - 額外可選填 email / remark
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=50)
    birthday: str
    class_: str = Field(..., min_length=1, alias="class")
    courses: list[PublicCourseItem] = Field(default=[], max_length=20)
    supplies: list[PublicSupplyItem] = Field(default=[], max_length=20)
    email: Optional[str] = Field(None, max_length=200)
    remark: str = ""
    school_year: Optional[int] = Field(None, ge=100, le=200)
    semester: Optional[int] = Field(None, ge=1, le=2)

    @field_validator("birthday")
    @classmethod
    def validate_birthday(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("生日格式必須為 YYYY-MM-DD")
        return v

    @field_validator("name", "class_", mode="before")
    @classmethod
    def strip_whitespace(cls, v):
        return v.strip() if isinstance(v, str) else v


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


def _invalidate_activity_dashboard_caches(
    session, *, summary_only: bool = False
) -> None:
    if summary_only:
        activity_service.invalidate_summary_cache(session)
        return
    activity_service.invalidate_dashboard_caches(session)


def _check_registration_open(session) -> None:
    """驗證報名時間是否開放，不符合時拋出 HTTPException。"""
    settings = session.query(ActivityRegistrationSettings).first()
    if settings:
        if not settings.is_open:
            raise HTTPException(status_code=400, detail="報名尚未開放")
        now_str = datetime.now(TAIPEI_TZ).replace(tzinfo=None).isoformat()
        if settings.open_at and now_str < settings.open_at:
            raise HTTPException(status_code=400, detail="報名尚未開始")
        if settings.close_at and now_str > settings.close_at:
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
    by_method 為 dict：{"現金": 1200, "轉帳": 500, ...}；method 為 NULL 者歸類為「未指定」。
    """
    rows = (
        session.query(
            ActivityPaymentRecord.type,
            ActivityPaymentRecord.payment_method,
            func.count(ActivityPaymentRecord.id),
            func.coalesce(func.sum(ActivityPaymentRecord.amount), 0),
        )
        .filter(ActivityPaymentRecord.payment_date == target_date)
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


def sync_registrations_on_student_deactivate(session, student_id: int) -> int:
    """學生畢業 / 退學 / 刪除時，軟刪該生當前學期啟用中 ActivityRegistration。

    - 把 is_active 設為 False；保留原 match_status（供後台稽核）
    - 只處理當前學期（歷史學期的報名維持原狀，仍可供報表追溯）
    - **若 paid_amount > 0**：自動寫一筆「系統補齊」退費沖帳紀錄並清零，
      並以 logger.warning 留痕提醒管理員處理實體退款；避免幽靈金額留存
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
    # 若今日已簽核且有任何一筆需沖帳，直接拋以維持 snapshot 一致性
    # （上層 students.py 的刪除流程會因此回 400；需先解鎖日結再刪學生）
    if any((r.paid_amount or 0) > 0 for r in regs):
        _require_daily_close_unlocked(session, today)
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
