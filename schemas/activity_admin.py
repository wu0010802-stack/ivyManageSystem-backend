"""schemas/activity_admin.py — 後台才藝管理 Pydantic schemas（F2 第三階段抽出）。

從 api/activity/_shared.py 抽出 Course / Supply CRUD 5 個 schemas：
- CourseCreate / CourseUpdate — 課程建立 / 更新（含 Phase 3 適齡 + 結構化時段）
- SupplyCreate / SupplyUpdate — 用品建立 / 更新
- CopyCoursesRequest — 一鍵複製上學期課程

api/activity/_shared.py re-export 維持 api/activity/courses.py / supplies.py
等模組的既有 import surface。
"""

from datetime import time
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# F2-aux：常數集中到 utils/activity_constants.py，避免重複宣告與 typo regression
# （第三階段曾因雙份宣告把 999_999 typo 成 99_999 → 課程/用品價超 99K 被誤拒）。
from utils.activity_constants import (
    MAX_PAYMENT_AMOUNT,
    MIN_REFUND_REASON_LENGTH,
    MIN_VOID_REASON_LENGTH,
)

# 允許的 video_url scheme（前端 :href 直出，禁 javascript:/data: 等避免儲存型 XSS）
_ALLOWED_VIDEO_URL_SCHEMES = {"http", "https"}


def _validate_video_url_scheme(v: Optional[str]) -> Optional[str]:
    """非空 video_url 的 scheme 須為 http/https；空字串/None 放行。"""
    if v is None or v == "":
        return v
    from urllib.parse import urlparse

    scheme = urlparse(v).scheme.lower()
    if scheme not in _ALLOWED_VIDEO_URL_SCHEMES:
        raise ValueError("video_url 僅允許 http 或 https 連結")
    return v


def validate_phase3_ranges(
    min_age_months: Optional[int],
    max_age_months: Optional[int],
    meeting_start_time: Optional[time],
    meeting_end_time: Optional[time],
) -> None:
    """Phase 3 適齡 / 時段範圍一致性檢核（成對欄位皆有值才比較）。

    供 CourseCreate / CourseUpdate 的 model_validator 與 update_course endpoint
    共用——endpoint 將 patch 合併 DB 現值後再呼叫此函式，避免部分更新（只動一邊）
    寫出 min_age>max_age 或 start>=end 的矛盾狀態（Finding 6）。

    Raises:
        ValueError：範圍矛盾時。
    """
    if (
        min_age_months is not None
        and max_age_months is not None
        and min_age_months > max_age_months
    ):
        raise ValueError("min_age_months 不可大於 max_age_months")
    if (
        meeting_start_time is not None
        and meeting_end_time is not None
        and meeting_start_time >= meeting_end_time
    ):
        raise ValueError("meeting_start_time 必須早於 meeting_end_time")


class CourseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    price: int = Field(..., ge=0, le=MAX_PAYMENT_AMOUNT)
    sessions: Optional[int] = Field(None, ge=1)
    capacity: int = Field(30, ge=1)
    video_url: Optional[str] = None
    allow_waitlist: bool = True
    description: Optional[str] = None
    # 學期（不指定時 API 端會用當前學期填入）
    school_year: Optional[int] = Field(None, ge=100, le=200)
    semester: Optional[int] = Field(None, ge=1, le=2)
    # Phase 3 適齡 + 結構化時段（前台 advisory）
    min_age_months: Optional[int] = Field(None, ge=0, le=360)
    max_age_months: Optional[int] = Field(None, ge=0, le=360)
    meeting_weekday: Optional[int] = Field(None, ge=0, le=6)
    meeting_start_time: Optional[time] = None
    meeting_end_time: Optional[time] = None
    instructor_name: Optional[str] = Field(None, max_length=50)

    _validate_video_url = field_validator("video_url")(_validate_video_url_scheme)

    @model_validator(mode="after")
    def _validate_phase3(self):
        validate_phase3_ranges(
            self.min_age_months,
            self.max_age_months,
            self.meeting_start_time,
            self.meeting_end_time,
        )
        return self


class CourseUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    price: Optional[int] = Field(None, ge=0, le=MAX_PAYMENT_AMOUNT)
    sessions: Optional[int] = Field(None, ge=1)
    capacity: Optional[int] = Field(None, ge=1)
    video_url: Optional[str] = None
    allow_waitlist: Optional[bool] = None
    description: Optional[str] = None
    # Phase 3 同上
    min_age_months: Optional[int] = Field(None, ge=0, le=360)
    max_age_months: Optional[int] = Field(None, ge=0, le=360)
    meeting_weekday: Optional[int] = Field(None, ge=0, le=6)
    meeting_start_time: Optional[time] = None
    meeting_end_time: Optional[time] = None
    instructor_name: Optional[str] = Field(None, max_length=50)

    _validate_video_url = field_validator("video_url")(_validate_video_url_scheme)

    @model_validator(mode="after")
    def _validate_phase3(self):
        validate_phase3_ranges(
            self.min_age_months,
            self.max_age_months,
            self.meeting_start_time,
            self.meeting_end_time,
        )
        return self


class SupplyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    price: int = Field(..., ge=0, le=MAX_PAYMENT_AMOUNT)
    school_year: Optional[int] = Field(None, ge=100, le=200)
    semester: Optional[int] = Field(None, ge=1, le=2)


class SupplyUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    price: Optional[int] = Field(None, ge=0, le=MAX_PAYMENT_AMOUNT)


class CopyCoursesRequest(BaseModel):
    """一鍵複製上學期課程到新學期的請求。"""

    source_school_year: int = Field(..., ge=100, le=200)
    source_semester: int = Field(..., ge=1, le=2)
    target_school_year: int = Field(..., ge=100, le=200)
    target_semester: int = Field(..., ge=1, le=2)


# ───────────────────────────────────────────────────────────────────────────
# F2 第四階段：剩餘 admin schemas
# ───────────────────────────────────────────────────────────────────────────

import re  # noqa: E402
from datetime import date  # noqa: E402

from pydantic import ConfigDict, field_validator  # noqa: E402
from typing import List, Literal  # noqa: E402

from schemas.activity_public import (  # noqa: E402
    PublicCourseItem,
    PublicSupplyItem,
    _validate_birthday_str,
)
from utils.taipei_time import validate_payment_date  # noqa: E402


class PaymentUpdate(BaseModel):
    is_paid: bool
    # is_paid=False 時必填，且必須等於當前 paid_amount；避免誤觸按鈕一次把該筆全退
    confirm_refund_amount: Optional[int] = Field(
        None,
        ge=0,
        le=MAX_PAYMENT_AMOUNT,
        description=(
            "當 is_paid=False 時必填，且需等於當前 paid_amount；"
            "供前端明確二次確認沖帳金額，避免誤操作整筆退費"
        ),
    )
    refund_reason: Optional[str] = Field(
        None,
        max_length=200,
        description=f"當 is_paid=False 時必填，≥ {MIN_REFUND_REASON_LENGTH} 字；留於沖帳紀錄 notes",
    )
    # is_paid=True 補齊路徑（shortfall > 0）時必填：人工收款方式（不可 SYSTEM_RECONCILE_METHOD）
    # 與 ≥5 字原因；handler 端會檢查並在大額時要求 ACTIVITY_PAYMENT_APPROVE。
    # Why: 原設計 is_paid=True 直接寫一筆「系統補齊」payment 補上欠費，沒有 method/原因/
    # 簽核，會計可逐筆把欠費轉成收入流水。對齊 is_paid=False 路徑的嚴格度。
    payment_method: Optional[Literal["現金"]] = Field(
        None,
        description=(
            "is_paid=True 補齊欠費時必填，必須為「現金」"
            "（目前才藝僅收現金），不接受『系統補齊』；handler 端會驗證"
        ),
    )
    payment_reason: Optional[str] = Field(
        None,
        max_length=200,
        description=f"is_paid=True 補齊欠費時必填，≥ {MIN_REFUND_REASON_LENGTH} 字；會寫進補齊紀錄 notes",
    )


class RemarkUpdate(BaseModel):
    remark: str


class VoidPaymentRequest(BaseModel):
    """軟刪除 payment 紀錄的請求；reason 必填且 ≥ MIN_VOID_REASON_LENGTH 字。

    不接受 DELETE 直接 body-less，避免一線員工順手按到就抹掉稽核。
    """

    reason: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=f"軟刪原因，最少 {MIN_VOID_REASON_LENGTH} 個字",
    )

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, v: str) -> str:
        cleaned = (v or "").strip()
        if len(cleaned) < MIN_VOID_REASON_LENGTH:
            raise ValueError(f"軟刪原因需至少 {MIN_VOID_REASON_LENGTH} 個字，不可敷衍")
        return cleaned


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
    # 只允許 True（批次補齊已繳費）；False 全額沖帳路徑已被收緊，改走單筆端點
    # 並附明確 confirm_refund_amount，避免誤操作一次沖一批
    is_paid: Literal[True]
    # 批次補齊會把欠費直接寫成系統 payment 流水，金額槓桿大；強制填原因防止濫用
    # （例如「2026-04-25 期末補繳整批，已收齊現金，老闆確認」）
    reason: str = Field(
        ...,
        min_length=MIN_REFUND_REASON_LENGTH,
        max_length=200,
        description=f"批次標記原因（≥ {MIN_REFUND_REASON_LENGTH} 字），會寫進每筆系統補齊紀錄的 notes",
    )

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, v: str) -> str:
        cleaned = (v or "").strip()
        if len(cleaned) < MIN_REFUND_REASON_LENGTH:
            raise ValueError(
                f"批次標記原因需至少 {MIN_REFUND_REASON_LENGTH} 個字，不可敷衍"
            )
        return cleaned


class AddPaymentRequest(BaseModel):
    type: Literal["payment", "refund"] = "payment"
    amount: int = Field(
        ...,
        gt=0,
        le=MAX_PAYMENT_AMOUNT,
        description=f"金額（正整數，上限 NT${MAX_PAYMENT_AMOUNT:,}；type 決定方向）",
    )
    payment_date: date
    payment_method: Literal["現金"] = Field(
        "現金",
        description="目前才藝 POS 僅支援現金；保留欄位供未來擴充",
    )
    notes: str = Field("", max_length=200)

    @model_validator(mode="after")
    def _refund_requires_reason(self):
        """type=refund 時 notes（原因）必填且 ≥ MIN_REFUND_REASON_LENGTH 字。"""
        if self.type == "refund":
            cleaned = (self.notes or "").strip()
            if len(cleaned) < MIN_REFUND_REASON_LENGTH:
                raise ValueError(
                    f"退費必須於 notes 填寫原因（至少 {MIN_REFUND_REASON_LENGTH} 個字）"
                )
        return self

    idempotency_key: str = Field(
        ...,
        description=(
            "冪等 key（8-64 英數/底線/連字號，必填）；同 key 在 10 分鐘內視為重試"
            "並回傳先前結果。Finding #1：所有金流寫入強制帶 key，移除模糊的內容式去重。"
        ),
    )

    @field_validator("payment_date")
    @classmethod
    def _validate_payment_date(cls, v: date) -> date:
        return validate_payment_date(v)

    @field_validator("idempotency_key")
    @classmethod
    def _validate_idk(cls, v: str) -> str:
        if not v or not re.match(r"^[A-Za-z0-9_-]{8,64}$", v):
            raise ValueError("idempotency_key 必填且格式須為 8-64 英數/底線/連字號")
        return v


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
        return _validate_birthday_str(v)

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
        return _validate_birthday_str(v)

    @field_validator("name", "class_", mode="before")
    @classmethod
    def strip_whitespace(cls, v):
        return v.strip() if isinstance(v, str) else v


class RefundCalcPayload(BaseModel):
    """退費計算稽核明細（calc_method 對應的形狀）。

    取代原裸 dict（OpenAPI additionalProperties:true → 前端 Record<string,
    unknown>）。course 比例法欄位較全；supply（不退）/未知總堂數法只有共同的
    amount_due + formula，其餘留 None。三種 calc_method 皆無此外的鍵，故以
    全欄位聯集 + Optional 即可結構化覆蓋。
    """

    # 三種 calc_method 共同欄位
    amount_due: int
    formula: str
    # 僅 activity_course_ratio（按出席堂數三段比例）才有
    T_total: Optional[int] = None
    T_served: Optional[int] = None
    served_ratio: Optional[float] = None
    ratio_band: Optional[str] = None
    refund_ratio: Optional[str] = None


class RefundSuggestionItem(BaseModel):
    """單一退費 item（course 或 supply）建議值。spec §7。"""

    type: str = Field(..., description="course | supply")
    target_id: int = Field(..., description="course_id 或 supply_id")
    name: str
    amount_due: int
    # NULL sessions 時為 None；前端應 fallback 顯示為「無法計算，建議全退」
    suggested_amount: Optional[int] = Field(None, description="None=無法計算")
    calc_method: str
    calc_payload: RefundCalcPayload
    warnings: list[str] = Field(default_factory=list)


class RefundSuggestionResponse(BaseModel):
    """GET /registrations/{id}/refund-suggestion 回應 schema。spec §7。"""

    registration_id: int
    computed_at: str  # ISO datetime
    # 算法見 spec §6：item.suggested 為 None 時以 amount_due fallback 加總
    total_suggested_amount: int
    total_amount_due: int
    items: list[RefundSuggestionItem]


# ───────────────────────────────────────────────────────────────────────────
# Phase 3.5 — api/activity/courses.py response_model
#
# 後台課程管理 8 個 endpoint 對應 Out schemas。
# 命名 prefix `Course` 避免與 schemas/activity_public.py 的 `PublicCourses*`
# 衝突（後者為公開報名前台用）。
# ───────────────────────────────────────────────────────────────────────────

from schemas._base import IvyBaseModel  # noqa: E402


class CourseListItemOut(IvyBaseModel):
    """GET /courses 單筆（含報名統計）。

    router 已把 meeting_start_time / meeting_end_time 序列化成 "HH:MM" str
    （與 PublicCoursesItemOut 同 pattern）；此處用 Optional[str] 直接接，
    避免 from_attributes 對應到 ORM Time 又被再序列化成 ISO。
    """

    id: int
    name: str
    price: int
    sessions: Optional[int] = None
    capacity: int
    video_url: str
    allow_waitlist: bool
    description: str
    school_year: int
    semester: int
    min_age_months: Optional[int] = None
    max_age_months: Optional[int] = None
    meeting_weekday: Optional[int] = None
    meeting_start_time: Optional[str] = None
    meeting_end_time: Optional[str] = None
    instructor_name: Optional[str] = None
    enrolled: int
    promoted_pending: int
    waitlist_count: int
    remaining: int


class CourseListOut(IvyBaseModel):
    """GET /courses 分頁回應（含 total + 學期 echo）。"""

    courses: list[CourseListItemOut]
    total: int
    skip: int
    limit: int
    school_year: int
    semester: int


class CourseDetailOut(IvyBaseModel):
    """GET /courses/{course_id} 詳情（不含報名統計）。

    router 目前未回 sessions/school_year/semester 等欄（與 list 不同）；
    保留現狀避免 silent strip。
    """

    id: int
    name: str
    price: int
    sessions: Optional[int] = None
    capacity: Optional[int] = None
    video_url: str
    allow_waitlist: bool
    description: str
    instructor_name: Optional[str] = None


class CourseCreateResultOut(IvyBaseModel):
    """POST /courses 201 回應。

    與 MutationResultOut 差異：多 school_year / semester 兩個 echo 欄位
    （前端建立後立即用此值切換 list 視圖）。
    """

    message: str
    id: int
    school_year: int
    semester: int


class CoursesCopyResultOut(IvyBaseModel):
    """POST /courses/copy-from-previous 201 回應。

    來源學期無課程時走 short-circuit：created=0, skipped=0, created_ids=[]。
    """

    message: str
    created: int
    skipped: int
    created_ids: list[int]


class CourseWaitlistItemOut(IvyBaseModel):
    """GET /courses/{course_id}/waitlist 單筆候補名單條目。"""

    waitlist_position: int
    course_record_id: int
    registration_id: int
    student_name: str  # pii-allow: 後台 ACTIVITY_READ 必看報名學生姓名
    class_name: Optional[str] = None


class CourseWaitlistOut(IvyBaseModel):
    """GET /courses/{course_id}/waitlist 完整回應。"""

    course_id: int
    course_name: str
    items: list[CourseWaitlistItemOut]


class CourseEnrolledItemOut(IvyBaseModel):
    """GET /courses/{course_id}/enrolled 單筆正式報名名單條目。"""

    position: int
    course_record_id: int
    registration_id: int
    student_name: str  # pii-allow: 後台 ACTIVITY_READ 必看報名學生姓名
    class_name: Optional[str] = None


class CourseEnrolledOut(IvyBaseModel):
    """GET /courses/{course_id}/enrolled 完整回應。"""

    course_id: int
    course_name: str
    items: list[CourseEnrolledItemOut]


# ───────────────────────────────────────────────────────────────────────────
# api/activity/supplies.py response_model（K5，2026-06-13）
#
# PUT / DELETE 直接重用 _common.DeleteResultOut（{message}），與 courses 同慣例。
# ───────────────────────────────────────────────────────────────────────────


class SupplyListItemOut(IvyBaseModel):
    """GET /supplies 單筆。

    school_year / semester 由 list 端點以 resolve 後的學期等值過濾，
    回傳值必為 int（NULL 列不會被過濾條件選中）。
    """

    id: int
    name: str
    price: int
    school_year: int
    semester: int


class SupplyListOut(IvyBaseModel):
    """GET /supplies 分頁回應（含 total + 學期 echo）。"""

    supplies: list[SupplyListItemOut]
    total: int
    skip: int
    limit: int
    school_year: int
    semester: int


class SupplyCreateResultOut(IvyBaseModel):
    """POST /supplies 201 回應（同 CourseCreateResultOut shape）。"""

    message: str
    id: int
    school_year: int
    semester: int


# ───────────────────────────────────────────────────────────────────────────
# Phase 3.5 — api/activity/registrations.py 9 endpoint response_model
#
# 重用 _common.MutationResultOut/DeleteResultOut 解三個 mutation；其餘四個自訂
# shape（admin_create / list / detail / basic update / sweep）。datetime 欄位皆
# 由 router 端 .isoformat() 後傳出 → 一律 Optional[str]（對齊 PublicCoursesItemOut
# meeting_*_time 同 trap）。PII 欄位（student_name / birthday / parent_phone /
# email / student_id / classroom_id）皆加 # pii-allow: 註解。
# ───────────────────────────────────────────────────────────────────────────


class RegistrationCreateResultOut(IvyBaseModel):
    """POST /registrations admin 新增報名回應（含候補資訊）。"""

    message: str
    id: int
    waitlisted: bool
    waitlist_courses: list[str]


class RegistrationListItemOut(IvyBaseModel):
    """GET /registrations items[] 單筆。

    缺 STUDENTS_READ / GUARDIANS_READ 對應 PII 欄會被 router 端遮成 None
    （student_id / birthday / classroom_id / parent_phone / email）。
    """

    id: int
    student_name: str  # pii-allow: 後台才藝報名列表家長/學生顯示
    student_id: Optional[int] = None  # pii-allow: 後台才藝報名列表家長/學生顯示
    birthday: Optional[str] = None  # pii-allow: 後台才藝報名列表家長/學生顯示
    class_name: Optional[str] = None
    classroom_id: Optional[int] = None  # pii-allow: 後台才藝報名列表家長/學生顯示
    parent_phone: Optional[str] = None  # pii-allow: 後台才藝報名列表家長/學生顯示
    match_status: Optional[str] = None
    pending_review: Optional[bool] = None
    is_active: bool
    email: Optional[str] = None  # pii-allow: 後台才藝報名列表家長/學生顯示
    is_paid: bool
    paid_amount: int
    total_amount: int
    payment_status: str
    remark: str
    school_year: Optional[int] = None
    semester: Optional[int] = None
    course_count: int
    supply_count: int
    course_names: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None


class RegistrationListOut(IvyBaseModel):
    """GET /registrations 列表 + 分頁 + 學期 echo。"""

    items: list[RegistrationListItemOut]
    total: int
    skip: int
    limit: int
    school_year: Optional[int] = None
    semester: Optional[int] = None


class RegistrationDetailCourseOut(IvyBaseModel):
    """GET /registrations/{id} courses[] 單筆。"""

    id: int
    course_id: int
    name: str
    price: int
    status: str
    confirm_deadline: Optional[str] = None


class RegistrationDetailSupplyOut(IvyBaseModel):
    """GET /registrations/{id} supplies[] 單筆。"""

    id: int
    supply_id: int
    name: str
    price: int


class RegistrationDetailChangeOut(IvyBaseModel):
    """GET /registrations/{id} changes[] 單筆稽核紀錄。"""

    id: int
    change_type: str
    description: str
    changed_by: Optional[str] = None
    created_at: Optional[str] = None


class RegistrationDetailOut(IvyBaseModel):
    """GET /registrations/{id} 詳情（含 courses / supplies / changes 三 nested list）。"""

    id: int
    student_name: str  # pii-allow: 後台才藝報名詳情家長/學生顯示
    student_id: Optional[int] = None  # pii-allow: 後台才藝報名詳情家長/學生顯示
    birthday: Optional[str] = None  # pii-allow: 後台才藝報名詳情家長/學生顯示
    class_name: Optional[str] = None
    classroom_id: Optional[int] = None  # pii-allow: 後台才藝報名詳情家長/學生顯示
    parent_phone: Optional[str] = None  # pii-allow: 後台才藝報名詳情家長/學生顯示
    match_status: Optional[str] = None
    pending_review: Optional[bool] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    email: Optional[str] = None  # pii-allow: 後台才藝報名詳情家長/學生顯示
    is_paid: bool
    paid_amount: int
    payment_status: str
    remark: str
    courses: list[RegistrationDetailCourseOut]
    supplies: list[RegistrationDetailSupplyOut]
    changes: list[RegistrationDetailChangeOut]
    total_amount: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class RegistrationBasicUpdateResultOut(IvyBaseModel):
    """PUT /registrations/{id} 基本資料更新回應（含 diff 筆數）。"""

    message: str
    changed: int


class WaitlistSweepResultOut(IvyBaseModel):
    """POST /waitlist/sweep-expired 候補過期掃描結果。

    expired / reminded / final_reminded 由 activity_service.sweep_expired_pending_promotions
    回傳，含家長 T-24h 與 T-6h 提醒推送計數。
    """

    message: str
    expired: int
    reminded: int
    final_reminded: int


# ───────────────────────────────────────────────────────────────────────────
# Phase 3.5 — api/activity/pos_approval.py 8 endpoint response_model
#
# 老闆每日核對 POS 流水後簽核某日 + 解鎖 + 對帳 + 稽核 dashboard。
# 命名 prefix `Pos` 對齊既有 ActivityPosDailyClose 模型語義。
# operator/approver username 屬 staff 操作者識別，pii-allow。
# ───────────────────────────────────────────────────────────────────────────


class PosPendingDailyCloseItemOut(IvyBaseModel):
    """GET /pos/daily-close/pending pending[] 單筆。"""

    date: str
    transaction_count: int
    payment_total: int
    refund_total: int
    net_total: int


class PosPendingDailyClosesOut(IvyBaseModel):
    """GET /pos/daily-close/pending 完整回應。"""

    start_date: str
    end_date: str
    pending: list[PosPendingDailyCloseItemOut]


class PosDailyCloseOut(IvyBaseModel):
    """GET /pos/daily-close/{date} 回應。

    含已簽核（_serialize_close）與未簽核即時 preview（_live_preview）兩 path 共用 shape；
    未簽核時 is_approved=False / approver_username=None / approved_at=None /
    actual_cash_count=None / cash_variance=None。
    by_method 為 method→net_amount mapping（含「現金」key）。
    """

    date: str
    is_approved: bool
    status: str
    approver_username: Optional[str] = None  # pii-allow: 簽核者 username 為 staff 識別
    approved_at: Optional[str] = None
    note: Optional[str] = None
    payment_total: int
    refund_total: int
    net_total: int
    transaction_count: int
    by_method: dict[str, int]
    actual_cash_count: Optional[int] = None
    cash_variance: Optional[int] = None
    # 後端權威的盤點門檻判定：現金毛流量（payment_gross + refund_gross）≥ 門檻時為
    # True，前端據此決定 actual_cash_count 是否必填，避免前端用淨額自行推算與後端
    # approve 守衛（用毛流量）口徑不一致 → 確認後才 400。已簽核（_serialize_close）
    # 一律 False：盤點僅在簽核 pending 日時強制，唯讀檢視不需再 gate。
    cash_count_required: bool = False


class PosDailyCloseApproveOut(PosDailyCloseOut):
    """POST /pos/daily-close/{date} 201 回應。

    繼承 PosDailyCloseOut 並加上 warnings — 簽核者 = 當日 POS 操作者時的軟提醒。
    """

    warnings: list[str] = []


class PosDailyCloseLiveDiffOut(IvyBaseModel):
    """unlock_daily_close 回應內 live_diff 結構（spec H2）。

    解鎖後實況 vs 原 snapshot 差異，幫解鎖人即時掌握「為什麼帳變了」。
    compute_daily_snapshot 失敗時 router 端直接回 live_diff=None。
    """

    payment_total_diff: int
    refund_total_diff: int
    net_total_diff: int
    transaction_count_diff: int
    live_payment_total: int
    live_refund_total: int
    live_net_total: int
    live_transaction_count: int
    original_payment_total: int
    original_refund_total: int
    original_net_total: int
    original_transaction_count: int


class PosDailyCloseUnlockOut(IvyBaseModel):
    """DELETE /pos/daily-close/{date} 解鎖回應。

    notification_delivered 表示原簽核人是否有有效 LINE 綁定（active + line_user_id +
    line_follow_confirmed_at）；client 據此決定是否私下告知對方。
    """

    close_date: str
    unlocked_at: str
    is_admin_override: bool
    notification_delivered: bool
    live_diff: Optional[PosDailyCloseLiveDiffOut] = None


class PosReconciliationItemOut(IvyBaseModel):
    """GET /pos/reconciliation items[] 單筆。

    已簽核日用 snapshot、未簽核日即時算；expected_cash 來自 by_method[現金]，
    actual_cash / variance 僅已簽核日才有值。
    """

    date: str
    is_approved: bool
    status: str
    payment_total: int
    refund_total: int
    net_total: int
    transaction_count: int
    expected_cash: int
    actual_cash: Optional[int] = None
    variance: Optional[int] = None


class PosReconciliationTotalsOut(IvyBaseModel):
    """GET /pos/reconciliation totals 區段。

    variance_total 在區間內無任何已簽核日填現金盤點時為 None（沒任何 variance 可加）。
    """

    payment_total: int
    refund_total: int
    net_total: int
    variance_total: Optional[int] = None


class PosReconciliationOut(IvyBaseModel):
    """GET /pos/reconciliation 完整回應。"""

    start_date: str
    end_date: str
    items: list[PosReconciliationItemOut]
    totals: PosReconciliationTotalsOut


class PosUnlockEventItemOut(IvyBaseModel):
    """GET /audit/pos-unlock-events events[] 單筆。

    close_date 解析自 ApprovalLog.doc_id（YYYYMMDD int）；非法 doc_id 時為 None。
    """

    id: int
    close_date: Optional[str] = None
    action: str
    unlocker_username: Optional[str] = None  # pii-allow: 解鎖者 username 為 staff 識別
    unlocker_role: Optional[str] = None
    comment: Optional[str] = None
    occurred_at: Optional[str] = None


class PosUnlockEventsOut(IvyBaseModel):
    """GET /audit/pos-unlock-events 完整回應。"""

    days: int
    count: int
    events: list[PosUnlockEventItemOut]


class PosOperatorUserOut(IvyBaseModel):
    """list_operator_activity operators[].user 內嵌結構。

    無對應 User row 的 operator 字串以 user=null 回傳（前端紅標提醒已停用 / 共用殘留）。
    """

    id: int
    display_name: str  # pii-allow: 後台稽核 dashboard staff 識別
    role: Optional[str] = None
    employee_id: Optional[int] = None
    is_active: bool


class PosOperatorActivityItemOut(IvyBaseModel):
    """GET /audit/operator-activity operators[] 單筆。"""

    operator: str  # pii-allow: 後台稽核 dashboard staff 識別（POS 操作者 username）
    payment_count: int
    refund_count: int
    total_count: int
    last_activity_at: Optional[str] = None
    user: Optional[PosOperatorUserOut] = None


class PosOperatorActivityOut(IvyBaseModel):
    """GET /audit/operator-activity 完整回應。"""

    days: int
    count: int
    operators: list[PosOperatorActivityItemOut]


class PosCloseHistorySnapshotOut(IvyBaseModel):
    """GET /audit/pos-close-history snapshots[] 單筆（spec H3）。

    每次 unlock 前的完整快照（含 by_method JSON、現金盤點等），供「當時帳長什麼樣」稽核。
    """

    id: int
    close_date: str
    approver_username: Optional[str] = (
        None  # pii-allow: 原簽核者 username 為 staff 識別
    )
    approver_role: Optional[str] = None
    approved_at: Optional[str] = None
    approve_note: Optional[str] = None
    payment_total: int
    refund_total: int
    net_total: int
    transaction_count: int
    by_method: dict[str, int]
    actual_cash_count: Optional[int] = None
    cash_variance: Optional[int] = None
    unlocked_at: Optional[str] = None
    unlocked_by: Optional[str] = None  # pii-allow: 解鎖者 username 為 staff 識別
    unlocked_by_role: Optional[str] = None
    is_admin_override: bool
    unlock_reason: Optional[str] = None


class PosCloseHistoryOut(IvyBaseModel):
    """GET /audit/pos-close-history 完整回應。"""

    close_date: str
    count: int
    snapshots: list[PosCloseHistorySnapshotOut]


# ───────────────────────────────────────────────────────────────────────────
# Phase 3.5 — api/activity/registrations_pending.py 7 endpoint response_model
#
# 後台才藝報名審核工作流 7 個 endpoint：
# - list_pending_registrations → PendingRegistrationListOut
# - admin_search_students → PendingRegistrationsSearchStudentsOut
# - match_registration / reject_registration / restore_registration
#   共用 PendingRegistrationActionResultOut（{message, registration_id}）
# - rematch_registration → PendingRegistrationRematchResultOut（多 matched/field_changed）
# - force_accept_registration → PendingRegistrationForceAcceptResultOut
#   （多 matched 固定 False / forced 固定 True / field_changed）
#
# 不重用 _common.MutationResultOut，因 mutation 回傳是 `registration_id` 非 `id`，
# 重用會 silent rename 前端欄位。datetime 欄位由 router 端 .isoformat() →
# Optional[str]（同 RegistrationListItemOut trap）。PII 欄（student_name /
# birthday / parent_phone / email / classroom_id / student_id）皆加
# # pii-allow: 註解。
# ───────────────────────────────────────────────────────────────────────────


class PendingRegistrationItemOut(IvyBaseModel):
    """GET /registrations/pending items[] 單筆。

    對應 _serialize_pending_item 的輸出 dict shape：
    - 缺 STUDENTS_READ → birthday / classroom_id 被遮成 None
    - 缺 GUARDIANS_READ → parent_phone / email 被遮成 None
    """

    id: int
    student_name: str  # pii-allow: 後台才藝報名審核家長/學生顯示
    birthday: Optional[str] = None  # pii-allow: 後台才藝報名審核家長/學生顯示
    class_name: Optional[str] = None
    classroom_id: Optional[int] = None  # pii-allow: 後台才藝報名審核家長/學生顯示
    parent_phone: Optional[str] = None  # pii-allow: 後台才藝報名審核家長/學生顯示
    match_status: Optional[str] = None
    pending_review: Optional[bool] = None
    email: Optional[str] = None  # pii-allow: 後台才藝報名審核家長/學生顯示
    school_year: Optional[int] = None
    semester: Optional[int] = None
    remark: str
    created_at: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None


class PendingRegistrationListOut(IvyBaseModel):
    """GET /registrations/pending 列表 + 分頁 + 學期 + status echo。"""

    items: list[PendingRegistrationItemOut]
    total: int
    skip: int
    limit: int
    school_year: int
    semester: int
    status: str


class PendingRegistrationsSearchStudentItemOut(IvyBaseModel):
    """GET /students/search items[] 單筆（搜尋後台在籍學生）。

    F-027：caller 必有 STUDENTS_READ（否則 403），所有 PII 欄一律顯示。
    student_id 為學號字串（Student.student_id），id 為 PK int。
    """

    id: int
    student_id: Optional[str] = None  # pii-allow: 後台才藝審核需顯示學號
    name: str  # pii-allow: 後台才藝審核需顯示學生姓名
    birthday: Optional[str] = None  # pii-allow: 後台才藝審核需顯示生日（比對依據）
    classroom_id: Optional[int] = None  # pii-allow: 後台才藝審核需顯示班級
    classroom_name: Optional[str] = None
    parent_phone: Optional[str] = (
        None  # pii-allow: 後台才藝審核需顯示家長手機（比對依據）
    )


class PendingRegistrationsSearchStudentsOut(IvyBaseModel):
    """GET /students/search 完整回應（僅 items，無分頁；router 端 limit 上限 50）。"""

    items: list[PendingRegistrationsSearchStudentItemOut]


class PendingRegistrationActionResultOut(IvyBaseModel):
    """match / reject / restore 共用 {message, registration_id} shape。

    不用 _common.MutationResultOut 因為欄位名為 `registration_id` 非 `id`
    （重用會 silent rename 前端欄位）。
    """

    message: str
    registration_id: int


class PendingRegistrationRematchResultOut(IvyBaseModel):
    """POST /registrations/{id}/rematch 回應。

    matched=True 代表三欄比對成功；field_changed=True 代表 body 帶入新欄位修正。
    """

    message: str
    matched: bool
    field_changed: bool
    registration_id: int


class PendingRegistrationForceAcceptResultOut(IvyBaseModel):
    """POST /registrations/{id}/force-accept 回應。

    matched 永遠 False（強制收件跳過比對），forced 永遠 True，
    field_changed 代表 body 是否同時修正三欄。
    """

    message: str
    matched: bool
    forced: bool
    field_changed: bool
    registration_id: int


# ───────────────────────────────────────────────────────────────────────────
# Phase 3.5 — api/activity/attendance.py 5/7 endpoint response_model
#
# 才藝課程出席管理 7 個 endpoint，其中 2 個回 StreamingResponse 暫免：
# - list_sessions                → ActivitySessionListOut
# - create_session               → ActivitySessionCreateResultOut
# - delete_session               → ActivitySessionDeleteResultOut（{ok: bool}）
# - get_session_detail           → ActivitySessionDetailOut（含 students + 可選 groups）
# - export_session_attendance    → defer（Excel StreamingResponse）
# - print_session_roll_pdf       → defer（PDF StreamingResponse）
# - batch_update_attendance      → ActivityAttendanceBatchUpdateResultOut
#
# datetime 欄位皆由 router 端 .isoformat() 後傳出 → Optional[str]（同 trap）。
# 學生姓名 / 班級 / student_id / classroom_id 為才藝出席必顯欄位，加 # pii-allow:。
# students/groups 共用 ActivitySessionStudentItemOut，避免巢狀重複定義。
# delete_session 回 {"ok": True} 與 _common.OkStatusOut/DeleteResultOut 欄名皆不
# 相同（後者為 status:str / message:str），自訂 ActivitySessionDeleteResultOut。
# ───────────────────────────────────────────────────────────────────────────


class ActivitySessionListItemOut(IvyBaseModel):
    """GET /attendance/sessions items[] 單筆（含出席統計）。

    course_name 為 ActivityCourse.name 顯示，非 PII（課程名）。
    created_by 為操作者 username，非家長/學生 PII。
    """

    id: int
    course_id: int
    course_name: str
    session_date: Optional[str] = None
    notes: str
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    recorded_count: int
    present_count: int


class ActivitySessionListOut(IvyBaseModel):
    """GET /attendance/sessions 分頁回應。"""

    items: list[ActivitySessionListItemOut]
    total: int
    skip: int
    limit: int


class ActivitySessionCreateResultOut(IvyBaseModel):
    """POST /attendance/sessions 201 回應（單筆 session 完整欄位）。

    與 list item 同型但無統計欄（剛建立 recorded/present 皆 0，前端不依賴）。
    """

    id: int
    course_id: int
    course_name: str
    session_date: str
    notes: str
    created_by: Optional[str] = None
    created_at: Optional[str] = None


class ActivitySessionBatchCreateResultOut(IvyBaseModel):
    """POST /attendance/sessions/batch 回應：依上課星期展開日期範圍批次建立場次。

    created_dates 為實際新建的日期（ISO 升冪）；已存在（uq course+date）者計入
    skipped_existing 而不報錯，讓重複按或微調範圍重跑為冪等。
    """

    course_id: int
    course_name: str
    weekday: int  # 0=Mon .. 6=Sun
    start_date: str
    end_date: str
    created_count: int
    skipped_existing: int
    created_dates: list[str]


class ActivitySessionDeleteResultOut(IvyBaseModel):
    """DELETE /attendance/sessions/{id} 回應。

    沿用既有 {"ok": True} shape，未改商業邏輯。
    不用 _common.OkStatusOut（欄位是 status:str）或 DeleteResultOut（message:str），
    重用會 silent rename 前端欄位。
    """

    ok: bool


class ActivitySessionStudentItemOut(IvyBaseModel):
    """get_session_detail / groups[] 共用的學生點名行。

    is_present=None 代表「未點名」（與 False=缺席 必須區分，前端依此決定顯示）。
    class_name 由 router 端融合：優先真實 Classroom.name，否則 ActivityRegistration.class_name 快照，皆無則空字串。
    """

    registration_id: int
    student_id: Optional[int] = None  # pii-allow: 才藝出席必顯學生 FK
    classroom_id: Optional[int] = None  # pii-allow: 才藝出席必顯班級
    student_name: str  # pii-allow: 才藝出席必顯學生姓名
    class_name: str
    is_present: Optional[bool] = None
    attendance_notes: str


class ActivitySessionGroupOut(IvyBaseModel):
    """get_session_detail group_by=classroom 時 groups[] 單筆。

    classroom_id=None 代表「未分班」（router 端永遠排在 groups 末尾）。
    """

    classroom_id: Optional[int] = None  # pii-allow: 才藝出席必顯班級
    classroom_name: str
    students: list[ActivitySessionStudentItemOut]


class ActivitySessionDetailOut(IvyBaseModel):
    """GET /attendance/sessions/{id} 詳情。

    groups 僅當 query group_by=classroom 時 router 才回，否則為 None。
    """

    id: int
    course_id: int
    course_name: str
    session_date: str
    notes: str
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    students: list[ActivitySessionStudentItemOut]
    total: int
    present_count: int
    absent_count: int
    groups: Optional[list[ActivitySessionGroupOut]] = None


class ActivityAttendanceBatchUpdateResultOut(IvyBaseModel):
    """PUT /attendance/sessions/{id}/records 批次點名 upsert 回應。

    updated = 實際寫入（既有報名）筆數；skipped = 已退課 / 已駁回 / 未報該課
    被過濾掉的筆數。前端據此顯示「成功 N 筆，跳過 M 筆」。
    """

    ok: bool
    updated: int
    skipped: int


# ───────────────────────────────────────────────────────────────────────────
# Phase 3.5 — api/activity/pos.py 5 endpoint response_model
#
# POS 快速收銀 5 端點：outstanding-by-student / checkout / daily-summary /
# recent-transactions / semester-reconciliation。print_pos_receipt_pdf
# 為 StreamingResponse 不接 response_model（defer）。
# 命名 prefix `Pos` 對齊既有 ActivityPosDailyClose 模型 + pos_approval 區塊。
# 既有 `PosReconciliationOut` 為日結區間對帳；此處 `PosSemesterReconciliationOut`
# 為學期對帳，名稱明確不衝突。
# 學生姓名 / birthday / 收據抬頭操作者 username 皆標 pii-allow。
# ───────────────────────────────────────────────────────────────────────────


class PosCourseDetailItemOut(IvyBaseModel):
    """POS 收據 / 列表內 courses[] 單筆。

    來源 `_fetch_reg_course_details`：name + price (price_snapshot) + status。
    與 `RegistrationDetailCourseOut` 不同（後者多帶 id/course_id/confirm_deadline）。
    """

    name: str
    price: int
    status: str


class PosSupplyDetailItemOut(IvyBaseModel):
    """POS 收據 / 列表內 supplies[] 單筆。

    來源 `_fetch_reg_supplies`：name + price (price_snapshot)。
    與 `RegistrationDetailSupplyOut` 不同（後者多帶 id/supply_id）。
    """

    name: str
    price: int


class PosOutstandingRegistrationItemOut(IvyBaseModel):
    """GET /pos/outstanding-by-student groups[].registrations[] 單筆。"""

    id: int
    total_amount: int
    paid_amount: int
    owed: int
    class_name: str
    courses: list[PosCourseDetailItemOut]
    supplies: list[PosSupplyDetailItemOut]
    created_at: Optional[str] = None


class PosOutstandingGroupOut(IvyBaseModel):
    """GET /pos/outstanding-by-student groups[] 單筆（依學生聚合）。

    student_key 為 `student_name|birthday` 字串，前端用作聚合鍵；
    缺 STUDENTS_READ 角色時 birthday 會被遮成 None。
    """

    student_key: str
    student_name: str  # pii-allow: POS 收銀必要顯示，無法遮蓋
    birthday: Optional[str] = (
        None  # pii-allow: birthday 為 String(20)，無 STUDENTS_READ 時為 None
    )
    class_name: str
    group_owed_total: int
    registrations: list[PosOutstandingRegistrationItemOut]


class PosOutstandingOut(IvyBaseModel):
    """GET /pos/outstanding-by-student 完整回應。

    truncated/total_active（M3）：底層查詢有防爆上限（_POS_LIST_QUERY_LIMIT），
    超限時 truncated=True 且 total_active 為過濾後全量筆數，避免無聲截斷。
    """

    groups: list[PosOutstandingGroupOut]
    truncated: bool = False
    total_active: int = 0


class PosCheckoutItemResultOut(IvyBaseModel):
    """POST /pos/checkout items[] 單筆（含 idempotent replay 兩 path）。"""

    registration_id: int
    student_name: str  # pii-allow: POS 收據必要顯示
    class_name: str
    amount_applied: int
    new_paid_amount: int
    total_amount: int
    new_payment_status: str
    courses: list[PosCourseDetailItemOut]
    supplies: list[PosSupplyDetailItemOut]


class PosCheckoutOut(IvyBaseModel):
    """POST /pos/checkout 201 回應。

    同 shape 涵蓋兩條 return path：
    1) 新建 receipt（line ~896 主回傳）
    2) idempotent replay（`_parse_receipt_response_from_record` 額外帶
       `idempotent_replay=True`；新建 path 為 None）
    tendered/change 僅現金且前端傳 tendered 時有值；replay path 永為 None。
    """

    receipt_no: str
    type: str  # "payment" | "refund"
    total: int
    tendered: Optional[int] = None
    change: Optional[int] = None
    payment_method: str
    payment_date: Optional[str] = None
    operator: str  # pii-allow: POS 操作員 username 為 staff 識別
    notes: str
    created_at: Optional[str] = None
    items: list[PosCheckoutItemResultOut]
    idempotent_replay: Optional[bool] = None
    # P2-6（2026-06-23 audit）：重印時若該收據有部分 item 已作廢，total 只計有效金額；
    # has_voided_items=True 時 original_total 為含作廢的原始開立金額，供前端/PDF 標註
    # 「部分作廢，原始金額 NT$X」。新建 checkout path 兩者為 False/None。
    has_voided_items: Optional[bool] = None
    original_total: Optional[int] = None


class PosDailySummaryByMethodItemOut(IvyBaseModel):
    """GET /pos/daily-summary by_method[] 單筆（依付款方式聚合）。

    來源 `compute_daily_snapshot` by_method_list — method/payment/refund/count。
    當日無交易時 caller 回傳空 list（非 dict）。
    """

    method: str
    payment: int
    refund: int
    count: int


class PosDailySummaryOut(IvyBaseModel):
    """GET /pos/daily-summary 回應。

    by_method 為 list（依付款方式聚合，員工只可輸入「現金」）；
    cash_warning=True 時前端顯示「請存銀行」橘色提示
    （cash_in_drawer >= cash_warning_threshold）。
    """

    date: str
    payment_total: int
    refund_total: int
    net: int
    payment_count: int
    refund_count: int
    by_method: list[PosDailySummaryByMethodItemOut]
    cash_in_drawer: int
    cash_warning: bool
    cash_warning_threshold: int
    # P2-5（2026-06-23 audit）：該日是否已日結簽核（ActivityPosDailyClose 存在）。
    # 已簽核日寫入被擋（live≡frozen），前端可據此顯示「已簽核」、必要時切到
    # reconciliation 凍結值。
    is_approved: bool = False


class PosRecentTransactionItemOut(IvyBaseModel):
    """GET /pos/recent-transactions transactions[].items[] 單筆。"""

    registration_id: int
    student_name: str  # pii-allow: POS 重印列表必要顯示
    class_name: str
    amount_applied: int
    new_paid_amount: int
    total_amount: int
    new_payment_status: str
    courses: list[PosCourseDetailItemOut]
    supplies: list[PosSupplyDetailItemOut]


class PosRecentTransactionOut(IvyBaseModel):
    """GET /pos/recent-transactions transactions[] 單筆。

    source: "pos" | "system"（系統沖帳 receipt_no 合成 SYS-<id>）。
    operator 僅 has_finance_approve 才有真實值，否則為 "[已遮罩]"。
    tendered/change 歷史無紀錄，永為 None。
    """

    receipt_no: str
    source: str
    type: str
    total: int
    tendered: Optional[int] = None
    change: Optional[int] = None
    payment_method: str
    payment_date: Optional[str] = None
    operator: str  # pii-allow: POS 操作員 username（無 ACTIVITY_PAYMENT_APPROVE 時為 "[已遮罩]"）
    notes: str
    created_at: Optional[str] = None
    student_names: list[str]  # pii-allow: POS 收據抬頭列表
    items: list[PosRecentTransactionItemOut]


class PosRecentTransactionsOut(IvyBaseModel):
    """GET /pos/recent-transactions 完整回應。"""

    date: str
    transactions: list[PosRecentTransactionOut]


class PosSemesterReconciliationItemOut(IvyBaseModel):
    """GET /pos/semester-reconciliation items[] 單筆。

    approval_status 四態：fully_approved / partially_approved /
    pending_approval / no_payment。offline_paid_amount 為非 POS 已繳差額
    （歷史匯入或直接寫入 paid_amount 的資料）。
    """

    id: int
    student_name: str  # pii-allow: 學期對帳必要顯示
    class_name: str
    is_active: bool
    course_names: list[str]
    total_amount: int
    paid_amount: int
    owed: int
    payment_status: str
    approval_status: str
    approved_paid_amount: int
    pending_paid_amount: int
    offline_paid_amount: int
    latest_payment_date: Optional[str] = None
    created_at: Optional[str] = None


class PosSemesterReconciliationTotalsOut(IvyBaseModel):
    """GET /pos/semester-reconciliation totals 區段。"""

    registration_count: int
    total_amount: int
    paid_amount: int
    outstanding_amount: int
    approved_paid_amount: int
    pending_paid_amount: int
    offline_paid_amount: int
    by_payment_status: dict[str, int]
    by_approval_status: dict[str, int]


class PosSemesterReconciliationOut(IvyBaseModel):
    """GET /pos/semester-reconciliation 完整回應（學期對帳總表，與 PosReconciliationOut
    日結區間對帳不同名不衝突）。

    truncated/total_active（M3）：底層查詢有防爆上限（_POS_LIST_QUERY_LIMIT），
    超限時 truncated=True 且 total_active 為過濾後 active 全量筆數，避免對帳
    總表無聲截斷。
    """

    school_year: int
    semester: int
    truncated: bool = False
    total_active: int = 0
    items: list[PosSemesterReconciliationItemOut]
    totals: PosSemesterReconciliationTotalsOut


# ───────────────────────────────────────────────────────────────────────────
# Phase 3.5 — api/activity/settings.py 5 endpoint response_model
#
# 報名時間設定 + 海報上傳 + 修改紀錄 + class-options 共 5 grandfather 條目：
# - get_registration_time   → ActivityRegistrationTimeOut（_serialize_settings shape）
# - update_registration_time → _common.DeleteResultOut（純 {message} mutation）
# - upload_activity_poster  → ActivityPosterUploadResultOut（{message, poster_url}）
# - get_changes             → ActivityRegistrationChangeListOut（含 student_name PII）
# - get_class_options       → ActivityClassOptionsOut（純班級名清單）
#
# 命名 prefix `ActivityRegistrationTime` / `ActivityPoster` /
# `ActivityRegistrationChange` / `ActivityClassOptions`，與既有 admin
# Registration 系列（PendingRegistrationListOut 等）不混。
# datetime 欄位 router 端 .isoformat() 後傳出 → Optional[str]（同既有 trap）。
# student_name 為後台稽核明確顯示報名學生（ACTIVITY_READ）→ # pii-allow:。
# ───────────────────────────────────────────────────────────────────────────


class ActivityRegistrationTimeOut(IvyBaseModel):
    """GET /settings/registration-time 回應。

    對應 _serialize_settings 輸出：未設定時 is_open=False，其餘欄位 None；
    已設定時各欄位來自 ActivityRegistrationSettings ORM。open_at / close_at
    在 ORM 為 String 欄位（ISO 8601），故為 Optional[str]。
    """

    is_open: bool
    open_at: Optional[str] = None
    close_at: Optional[str] = None
    page_title: Optional[str] = None
    term_label: Optional[str] = None
    event_date_label: Optional[str] = None
    target_audience: Optional[str] = None
    form_card_title: Optional[str] = None
    poster_url: Optional[str] = None


class ActivityPosterUploadResultOut(IvyBaseModel):
    """POST /settings/poster 海報上傳 200 回應。

    回 {message, poster_url}：poster_url 為 backend.public_url 產出的對外網址
    （local 模式：/api/activity/public/poster/<file>；supabase 模式：
    https://<project>.supabase.co/.../activity-posters/<file>）。
    不用 _common.MutationResultOut（後者欄位為 id）— 重用會 silent rename。
    """

    message: str
    poster_url: str


class ActivityRegistrationChangeItemOut(IvyBaseModel):
    """GET /changes items[] 單筆 RegistrationChange 稽核紀錄。

    student_name 為快照欄位（報名當下 snapshot），ACTIVITY_READ 後台顯示。
    changed_by 為操作者 username 或系統字串，非家長/學生 PII。
    """

    id: int
    registration_id: int
    student_name: str  # pii-allow: 後台才藝報名稽核紀錄顯示學生姓名
    change_type: str
    description: str
    changed_by: Optional[str] = None
    created_at: Optional[str] = None


class ActivityRegistrationChangeListOut(IvyBaseModel):
    """GET /changes 列表 + 總筆數。"""

    items: list[ActivityRegistrationChangeItemOut]
    total: int


class ActivityClassOptionsOut(IvyBaseModel):
    """GET /class-options 回應。

    options 為 Classroom.name 字串清單（filter is_active=True，依 id 排序）；
    僅班級代稱，非 PII。
    """

    options: list[str]


# ── 統計儀表板 stats Out schemas（api/activity/stats.py 4 個 JSON 端點）────────
# 2026-06-13：欄位名沿用既有前端契約（totalRevenue 等 camelCase key 不可改）。
# totalRevenue/totalUnpaid 為實收口徑：revenue = paid_amount 加總（含 partial/
# overpaid 照實）、unpaid = max(0, 應繳總額 - paid_amount) 加總。


class ActivityStatsSummaryOut(IvyBaseModel):
    """GET /stats-summary（學期感知；unreadInquiries 為全域收件匣不分學期）。"""

    totalRegistrations: int
    totalEnrollments: int
    totalWaitlist: int
    totalSupplyOrders: int
    todayNewRegistrations: int
    totalRevenue: int
    totalUnpaid: int
    enrollmentRate: float
    unreadInquiries: int


class ActivityStatsDailyPointOut(IvyBaseModel):
    """每日報名趨勢單點（date 為 YYYY-MM-DD 字串）。"""

    date: str
    count: int


class ActivityStatsTopCourseOut(IvyBaseModel):
    """熱門課程單筆（enrolled 報名數倒序 top 5）。"""

    name: str
    count: int


class ActivityStatsChartsOut(IvyBaseModel):
    """GET /stats-charts 回應。"""

    daily: list[ActivityStatsDailyPointOut]
    topCourses: list[ActivityStatsTopCourseOut]


class ActivityAttendanceCourseStatOut(IvyBaseModel):
    """單一課程出席率統計。"""

    course_name: str
    sessions: int
    avg_rate: float


class ActivityAttendanceStatsOut(IvyBaseModel):
    """get_attendance_stats 聚合（avg_attendance_rate 為 0~1 小數）。"""

    total_sessions: int
    avg_attendance_rate: float
    by_course: list[ActivityAttendanceCourseStatOut]


class ActivityStatsOut(IvyBaseModel):
    """GET /stats 相容舊版複合回應（summary + charts + 出席統計）。"""

    statistics: ActivityStatsSummaryOut
    charts: ActivityStatsChartsOut
    attendance_stats: ActivityAttendanceStatsOut


class ActivityDashboardCourseOut(IvyBaseModel):
    """dashboard-table 課程欄位定義（courses dict 的 key 為 str(course id)）。"""

    id: int
    name: str


class ActivityDashboardClassroomRowOut(IvyBaseModel):
    """dashboard-table 班級列。courses 為 {course_id(str): 報名數}。"""

    classroom_id: int
    classroom_name: str
    teacher_name: str
    student_count: int
    courses: dict[str, int]
    total_enrollments: int
    ratio: int


class ActivityDashboardGradeSubtotalOut(IvyBaseModel):
    """年級小計（bonus/points 為達標獎勵展示值）。"""

    student_count: int
    courses: dict[str, int]
    total_enrollments: int
    ratio: int
    bonus: int
    points: int


class ActivityDashboardGradeRowOut(IvyBaseModel):
    """dashboard-table 年級區塊。"""

    grade_id: int
    grade_name: str
    target_percent: int
    classrooms: list[ActivityDashboardClassroomRowOut]
    subtotal: ActivityDashboardGradeSubtotalOut


class ActivityDashboardGrandTotalOut(IvyBaseModel):
    """dashboard-table 全園總計。"""

    student_count: int
    courses: dict[str, int]
    total_enrollments: int
    ratio: int


class ActivityDashboardTableOut(IvyBaseModel):
    """GET /dashboard-table 回應（含學期 echo）。"""

    courses: list[ActivityDashboardCourseOut]
    grades: list[ActivityDashboardGradeRowOut]
    grand_total: ActivityDashboardGrandTotalOut
    school_year: int
    semester: int


# ── Quick Win B（2026-06-22）：補裸 dict 端點的 response_model ────────────
# 這些 admin 端點原回裸 dict → OpenAPI 無具名 schema → 前端 codegen 只能拿到
# unknown（且多在金流/報名異動最該有契約處）。下列 Out 欄位與既有 return dict
# 完全對齊，IvyBaseModel 保留 codegen 型別、不改 wire shape。{message}-only 回應
# 直接重用 schemas._common.DeleteResultOut，不在此重複定義。


# inquiries.py（家長提問）
class InquiryItemOut(IvyBaseModel):
    id: int
    name: Optional[str] = None
    phone: Optional[str] = (
        None  # pii-allow: 業主裁定 inquiry 聯絡電話保留明碼（公開報名查詢需回傳）
    )
    question: Optional[str] = None
    is_read: bool
    reply: Optional[str] = None
    replied_at: Optional[str] = None
    created_at: Optional[str] = None


class InquiryListOut(IvyBaseModel):
    items: list[InquiryItemOut]
    total: int
    unread_count: int


# registrations_items.py（報名項目增刪）
class AddCourseResultOut(IvyBaseModel):
    message: str
    status: str  # enrolled / waitlist
    total_amount: int
    paid_amount: int
    outstanding_amount: int
    payment_status: str


class AddSupplyResultOut(IvyBaseModel):
    message: str
    id: int
    total_amount: int
    paid_amount: int
    outstanding_amount: int
    payment_status: str


class RemoveItemResultOut(IvyBaseModel):
    """退課 / 退用品共用：含退費金額（force_refund 時 > 0，否則 0）。"""

    message: str
    total_amount: int
    paid_amount: int
    refunded_amount: int
    payment_status: str


# registrations_payments.py（繳退費）
class PaymentRecordItemOut(IvyBaseModel):
    id: int
    type: str  # payment / refund
    amount: int
    payment_date: Optional[str] = None
    payment_method: str
    notes: str
    operator: Optional[str] = None  # 無 ACTIVITY_PAYMENT_APPROVE 時遮罩為 None/遮罩字串
    created_at: Optional[str] = None
    is_voided: bool
    voided_at: Optional[str] = None
    voided_by: Optional[str] = None
    void_reason: str


class PaymentListOut(IvyBaseModel):
    total_amount: int
    paid_amount: int
    payment_status: str
    records: list[PaymentRecordItemOut]


class PaymentMutationOut(IvyBaseModel):
    """新增繳/退費回應（含冪等 replay 路徑，三條 return 同形）。paid_amount 取
    reg.paid_amount 直值，以 Optional 兜底避免極端 None 觸發 ResponseValidationError。"""

    message: str
    paid_amount: Optional[int] = None
    payment_status: str


class PaymentVoidResultOut(PaymentMutationOut):
    voided_at: Optional[str] = None


# registrations_static.py（批次付款）
class BatchPaymentResultOut(IvyBaseModel):
    message: str
    updated: int


# ═══════════════════════════════════════════════════════════════════════════
# 從 api/activity/ 各 router 內聯抽出的 request schemas（2026-06-24）
# Why: 才藝 request schema 收斂到 schemas/ 單一來源，與既有 admin schema 一致；
# router 只保留路由與 handler。驗證所需的 _shared 函式以 validator 內 lazy import
# 取用，避免 schemas/ ←→ api.activity._shared 的模組級循環匯入。
# ═══════════════════════════════════════════════════════════════════════════


# ── POS 結帳（原 api/activity/pos.py）──────────────────────────────────────
_MAX_TENDERED = 9_999_999  # 客戶實付上限 NT$9,999,999（避免整型誇張值）
_IDK_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")  # 冪等 key：8-64 英數/底線/連字號


class POSCheckoutItem(BaseModel):
    registration_id: int = Field(..., gt=0)
    amount: int = Field(
        ...,
        gt=0,
        le=MAX_PAYMENT_AMOUNT,
        description="本次此筆收取金額（正整數，上限 NT$999,999）",
    )


class POSCheckoutRequest(BaseModel):
    items: List[POSCheckoutItem] = Field(..., min_length=1, max_length=10)
    payment_method: Literal["現金"] = Field(
        "現金",
        description="目前 POS 僅支援現金；payment_method 欄位保留供未來擴充",
    )
    payment_date: date
    tendered: Optional[int] = Field(
        None,
        ge=0,
        le=_MAX_TENDERED,
        description="客戶實付（僅現金有意義，上限 NT$9,999,999）",
    )
    notes: str = Field("", max_length=200)
    type: Literal["payment", "refund"] = "payment"
    idempotency_key: str = Field(
        ...,
        description=(
            "冪等 key（必填），同 key 在 10 分鐘內重送視為重試，回傳先前結果。"
            "Finding #1：POS 結帳強制帶 key，移除無-key 內容式去重。"
        ),
    )

    @field_validator("payment_date")
    @classmethod
    def _validate_payment_date(cls, v: date) -> date:
        return validate_payment_date(v)

    @field_validator("idempotency_key")
    @classmethod
    def _validate_idempotency_key(cls, v: str) -> str:
        if not v or not _IDK_PATTERN.match(v):
            raise ValueError("idempotency_key 必填且格式須為 8-64 英數/底線/連字號")
        return v

    def refund_notes_cleaned(self) -> str:
        """回傳 cleaned notes（僅 type=refund 時使用；handler 額外呼叫
        require_refund_reason 做最終閘門）。"""
        return (self.notes or "").strip()


# ── 出席場次 / 點名（原 api/activity/attendance.py）─────────────────────────
class SessionCreate(BaseModel):
    course_id: int
    session_date: date
    notes: Optional[str] = None


class SessionBatchCreate(BaseModel):
    course_id: int
    start_date: date
    end_date: date
    # 省略則用課程 meeting_weekday；0=Mon..6=Sun
    weekday: Optional[int] = Field(None, ge=0, le=6)
    notes: Optional[str] = None


class AttendanceRecordItem(BaseModel):
    registration_id: int
    is_present: bool
    notes: Optional[str] = ""


class BatchAttendanceUpdate(BaseModel):
    records: List[AttendanceRecordItem] = Field(..., min_length=1, max_length=500)


# ── POS 日結簽核（原 api/activity/pos_approval.py）──────────────────────────
_UNLOCK_REASON_MIN_LENGTH = 10
_ADMIN_OVERRIDE_REASON_MIN_LENGTH = 30


class DailyCloseCreate(BaseModel):
    note: Optional[str] = Field(None, max_length=500)
    actual_cash_count: Optional[int] = Field(
        None, ge=0, le=9_999_999, description="實際現金盤點金額（可選）"
    )


class DailyCloseUnlock(BaseModel):
    """解鎖日結簽核的請求。

    一般 4-eye 路徑：reason ≥ 10 字 + 解鎖人 ≠ 原簽核人（handler 守衛）。
    Admin override 路徑：is_admin_override=True + reason ≥ 30 字 + role='admin'（handler 守衛）。

    Why: 原設計只擋 reason 長度，未限制「自簽自解」循環；spec C2 收緊。
    """

    reason: str = Field(..., max_length=500)
    is_admin_override: bool = Field(
        False,
        description=(
            "管理員緊急 override：略過 4-eye 但 reason 須 ≥ "
            f"{_ADMIN_OVERRIDE_REASON_MIN_LENGTH} 字"
        ),
    )

    @model_validator(mode="after")
    def _validate_reason_length(self):
        cleaned = (self.reason or "").strip()
        min_len = (
            _ADMIN_OVERRIDE_REASON_MIN_LENGTH
            if self.is_admin_override
            else _UNLOCK_REASON_MIN_LENGTH
        )
        if len(cleaned) < min_len:
            extra = (
                "（admin override 須具體說明緊急情況）"
                if self.is_admin_override
                else ""
            )
            raise ValueError(f"解鎖原因需至少 {min_len} 字{extra}")
        self.reason = cleaned
        return self


# ── 報名審核工作流（原 api/activity/registrations_pending.py）───────────────
class RegistrationMatchRequest(BaseModel):
    student_id: int = Field(..., gt=0)


class RegistrationRejectRequest(BaseModel):
    reason: str = Field(..., min_length=2, max_length=200)

    @field_validator("reason", mode="before")
    @classmethod
    def _strip_reason(cls, v):
        if isinstance(v, str):
            stripped = v.strip()
            if len(stripped) < 2:
                raise ValueError("拒絕原因至少需 2 個字，方便事後追溯")
            return stripped
        return v


class RegistrationRematchRequest(BaseModel):
    """重新比對可選欄位：校方可即時修正家長打錯的 name/birthday/parent_phone。

    三欄皆可選——未提供時沿用 registration 原值。提供的欄位會在比對前寫回 reg，
    即使比對仍失敗也保留修改內容，避免校方白打一次字。
    """

    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = Field(None, min_length=1, max_length=50)
    birthday: Optional[str] = None
    parent_phone: Optional[str] = Field(None, min_length=8, max_length=30)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v):
        if isinstance(v, str):
            stripped = v.strip()
            return stripped or None
        return v

    @field_validator("birthday")
    @classmethod
    def _validate_birthday(cls, v):
        if v is None or v == "":
            return None
        from datetime import date as _d

        try:
            _d.fromisoformat(v)
        except ValueError:
            raise ValueError("生日格式必須為 YYYY-MM-DD")
        return v

    @field_validator("parent_phone", mode="before")
    @classmethod
    def _normalize_phone(cls, v):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        from api.activity._shared import _validate_tw_mobile

        return _validate_tw_mobile(v)
