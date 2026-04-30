"""
api/activity/_shared.py — 才藝系統共用 schemas、helpers、常數
"""

import hashlib
import json
import re
import logging
from collections import defaultdict
from datetime import datetime, date, timedelta
from typing import Optional, List, Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request, Response
from fastapi.responses import Response as PlainResponse
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

# payment_date 合理範圍：最多回補 30 天、不得指定未來。
# POS checkout 與後台 /registrations/{id}/payments 共用，避免管理員透過後者繞過 POS 管制。
PAYMENT_DATE_BACK_LIMIT_DAYS = 30

# 退費必填原因最短字數（避免「.」或「退」等無意義敷衍）
MIN_REFUND_REASON_LENGTH = 5

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


def validate_payment_date(
    value: date, *, back_limit_days: int = PAYMENT_DATE_BACK_LIMIT_DAYS
) -> date:
    """驗證 payment_date 必須在今日回補窗內，不得指定未來。

    `back_limit_days` 預設 30（活動 POS 場景）；學費跨月分期需放寬，
    呼叫端可覆寫此參數。
    """
    today = datetime.now(TAIPEI_TZ).date()
    if value > today:
        raise ValueError("繳費日期不可指定未來日期")
    earliest = today - timedelta(days=back_limit_days)
    if value < earliest:
        raise ValueError(f"繳費日期超出範圍，最多回補 {back_limit_days} 天")
    return value


def today_taipei() -> date:
    """統一取「今日」的工具函式（Asia/Taipei）。

    Why: 部分端點寫入 refund/payment 時使用 naive datetime.now().date()；
    server 若部署在 UTC，近午夜台灣時間會落帳到昨天，與日結 snapshot 錯位。
    本函式確保所有 activity 相關寫入都以台灣時間為準。
    """
    return datetime.now(TAIPEI_TZ).date()


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
    price: int = Field(..., ge=0, le=MAX_PAYMENT_AMOUNT)
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
    price: Optional[int] = Field(None, ge=0, le=MAX_PAYMENT_AMOUNT)
    sessions: Optional[int] = Field(None, ge=1)
    capacity: Optional[int] = Field(None, ge=1)
    video_url: Optional[str] = None
    allow_waitlist: Optional[bool] = None
    description: Optional[str] = None


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
    payment_method: Optional[str] = Field(
        None,
        max_length=20,
        description=(
            "is_paid=True 補齊欠費時必填，必須為實際收款方式（如：現金/轉帳/其他），"
            "不接受『系統補齊』；handler 端會驗證"
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
    payment_method: Literal["現金", "轉帳", "其他"] = "現金"
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


class PublicCourseItem(BaseModel):
    # 只收 name：價格一律以後端 ActivityCourse.price 為準。
    # 前端若仍送 price 欄位，Pydantic 預設 extra='ignore' 會自動丟棄，
    # 避免維護者誤把 client 傳入金額當作實價使用（過去此處曾保留 price
    # 欄位，後端忽略它，但留下 code smell）。
    name: str


class PublicSupplyItem(BaseModel):
    # 同 PublicCourseItem：只收 name，價格一律以 DB 為準
    name: str


class PublicInquiryPayload(BaseModel):
    """LOW-4：附 honeypot（hp）+ 時間戳（ts）兩個 alias 欄位。"""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=50)
    phone: str = Field(..., min_length=1, max_length=30)
    question: str = Field(..., min_length=1, max_length=2000)
    hp: str = Field(default="", alias="_hp", max_length=200)
    ts: Optional[int] = Field(default=None, alias="_ts")


def should_silent_reject_bot(hp: str, ts: Optional[int]) -> bool:
    """LOW-4：honeypot + 時序檢查。回 True 表示「請當作機器人 silent reject」。

    判定條件（任一命中即視為 bot）：
    - 隱形 _hp 欄位被填入任何字元 → 真人看不到該欄位，bot 表單填充器會填
    - 提交時間距離頁面載入不到 3 秒 → 真人讀題作答幾乎不可能這麼快

    呼叫端應在判定為 bot 時：
    - 不寫 DB
    - 不發 LINE 推播
    - 仍回 200/201 + 正常成功訊息（不洩漏偵測）
    - log.warning 留痕方便事後分析
    """
    if hp:
        return True
    if ts is not None:
        try:
            now_ms = int(datetime.now(TAIPEI_TZ).timestamp() * 1000)
            elapsed_ms = now_ms - int(ts)
            if 0 <= elapsed_ms < 3000:
                return True
        except (ValueError, TypeError):
            pass
    return False


_TW_MOBILE_RE = re.compile(r"^09\d{8}$")


def _validate_birthday_str(v: str) -> str:
    """共用：生日格式 + 合理範圍檢查。

    - 格式必須為 YYYY-MM-DD
    - 不得為未來日期
    - 不得早於 20 年前（幼稚園/才藝學生涵蓋 0-18 歲，留 2 年緩衝）
    Why: 原本僅檢格式，家長可誤填 2099-01-01 或 1900 年之類資料，後續年齡/報表計算會錯亂。
    """
    try:
        bday = date.fromisoformat(v)
    except ValueError:
        raise ValueError("生日格式必須為 YYYY-MM-DD")
    today = datetime.now(TAIPEI_TZ).date()
    if bday > today:
        raise ValueError("生日不可為未來日期")
    if (today - bday).days > 20 * 366:
        raise ValueError("生日超出合理範圍")
    return v


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
    # LOW-4：honeypot + 提交時間戳（ms epoch）
    hp: str = Field(default="", alias="_hp", max_length=200)
    ts: Optional[int] = Field(default=None, alias="_ts")

    @field_validator("birthday")
    @classmethod
    def validate_birthday(cls, v: str) -> str:
        return _validate_birthday_str(v)

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
        return _validate_birthday_str(v)

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
    by_method 為 dict：{"現金": 1200, "轉帳": 500, ...}；method 為 NULL 者歸類為「未指定」。

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
