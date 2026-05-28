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

from pydantic import BaseModel, Field, model_validator

# F2-aux：常數集中到 utils/activity_constants.py，避免重複宣告與 typo regression
# （第三階段曾因雙份宣告把 999_999 typo 成 99_999 → 課程/用品價超 99K 被誤拒）。
from utils.activity_constants import (
    MAX_PAYMENT_AMOUNT,
    MIN_REFUND_REASON_LENGTH,
    MIN_VOID_REASON_LENGTH,
)


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

    @model_validator(mode="after")
    def _validate_phase3(self):
        if (
            self.min_age_months is not None
            and self.max_age_months is not None
            and self.min_age_months > self.max_age_months
        ):
            raise ValueError("min_age_months 不可大於 max_age_months")
        if (
            self.meeting_start_time is not None
            and self.meeting_end_time is not None
            and self.meeting_start_time >= self.meeting_end_time
        ):
            raise ValueError("meeting_start_time 必須早於 meeting_end_time")
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

    @model_validator(mode="after")
    def _validate_phase3(self):
        if (
            self.min_age_months is not None
            and self.max_age_months is not None
            and self.min_age_months > self.max_age_months
        ):
            raise ValueError("min_age_months 不可大於 max_age_months")
        if (
            self.meeting_start_time is not None
            and self.meeting_end_time is not None
            and self.meeting_start_time >= self.meeting_end_time
        ):
            raise ValueError("meeting_start_time 必須早於 meeting_end_time")
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
        description="當 is_paid=False 時必填，≥ 5 字；留於沖帳紀錄 notes",
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
        description="is_paid=True 補齊欠費時必填，≥ 5 字；會寫進補齊紀錄 notes",
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

    idempotency_key: Optional[str] = Field(
        None,
        description="冪等 key（8-64 英數/底線/連字號）；同 key 在 10 分鐘內視為重試並回傳先前結果",
    )

    @field_validator("payment_date")
    @classmethod
    def _validate_payment_date(cls, v: date) -> date:
        return validate_payment_date(v)

    @field_validator("idempotency_key")
    @classmethod
    def _validate_idk(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not re.match(r"^[A-Za-z0-9_-]{8,64}$", v):
            raise ValueError("idempotency_key 格式不合（需 8-64 英數/底線/連字號）")
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


class RefundSuggestionItem(BaseModel):
    """單一退費 item（course 或 supply）建議值。spec §7。"""

    type: str = Field(..., description="course | supply")
    target_id: int = Field(..., description="course_id 或 supply_id")
    name: str
    amount_due: int
    # NULL sessions 時為 None；前端應 fallback 顯示為「無法計算，建議全退」
    suggested_amount: Optional[int] = Field(None, description="None=無法計算")
    calc_method: str
    calc_payload: dict
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
