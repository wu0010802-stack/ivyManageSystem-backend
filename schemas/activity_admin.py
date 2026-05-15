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
