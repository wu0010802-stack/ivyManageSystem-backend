"""schemas/activity_public.py — 公開報名頁 Pydantic schemas（F2 第二階段抽出）。

從 api/activity/_shared.py 抽出 5 個公開報名 schemas + 共用驗證 helper：
- PublicCourseItem / PublicSupplyItem — 課程/用品 minimal payload（只收 name，價格以 DB 為準）
- PublicInquiryPayload — 諮詢表單（含 honeypot + ts）
- PublicRegistrationPayload — 公開報名 payload（honeypot + 學生資料 + 課程/用品）
- PublicUpdatePayload — 公開修改 payload（含 if_unmodified_since 樂觀鎖）

附帶驗證 helper:
- should_silent_reject_bot — honeypot + 時序判定
- _validate_birthday_str / _normalize_phone / _validate_tw_mobile

api/activity/_shared.py re-export 維持既有 import surface（api/activity/public.py
等 6+ 模組不需動）。
"""

import re
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas._base import IvyBaseModel
from utils.taipei_time import TAIPEI_TZ

# ─── minimal items ────────────────────────────────────────────────────────


class PublicCourseItem(BaseModel):
    # 只收 name：價格一律以後端 ActivityCourse.price 為準。
    # 前端若仍送 price 欄位，Pydantic 預設 extra='ignore' 會自動丟棄，
    # 避免維護者誤把 client 傳入金額當作實價使用（過去此處曾保留 price
    # 欄位，後端忽略它，但留下 code smell）。
    name: str


class PublicSupplyItem(BaseModel):
    # 同 PublicCourseItem：只收 name，價格一律以 DB 為準
    name: str


# ─── 共用驗證 helper（公開頁三 payload 都用）────────────────────────────


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

    注意：honeypot / 時序檢查為**輔助**手段、非主要 anti-automation；
    真正節流仰賴限流器（register 5/min、query 10/min）。
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


# ─── 公開 payloads ─────────────────────────────────────────────────────────


class PublicInquiryPayload(BaseModel):
    """LOW-4：附 honeypot（hp）+ 時間戳（ts）兩個 alias 欄位。"""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=50)
    phone: str = Field(..., min_length=1, max_length=30)
    question: str = Field(..., min_length=1, max_length=2000)
    hp: str = Field(default="", alias="_hp", max_length=200)
    ts: Optional[int] = Field(default=None, alias="_ts")


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
    # S3：對齊 register 版（PublicRegistrationPayload.remark）的 500 字上限
    remark: str = Field(default="", max_length=500)
    # 選填：樂觀鎖 token，由 /public/query 回傳的 updated_at（ISO 字串）。
    # 提供時若與 reg.updated_at 不符即拒；不提供則沿用舊行為（向後相容）。
    if_unmodified_since: Optional[str] = Field(None, max_length=64)

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


# ─── 公開端點 response_model（Phase 3.5） ─────────────────────────────────


class PublicRegistrationTimeOut(IvyBaseModel):
    """GET /public/registration-time response。

    所有顯示欄位皆 Optional：settings 為 None 時整批回 None。
    """

    is_open: bool
    open_at: Optional[datetime] = None
    close_at: Optional[datetime] = None
    page_title: Optional[str] = None
    term_label: Optional[str] = None
    event_date_label: Optional[str] = None
    target_audience: Optional[str] = None
    form_card_title: Optional[str] = None
    poster_url: Optional[str] = None


class PublicCoursesItemOut(IvyBaseModel):
    """GET /public/courses 單筆。

    router 已把 meeting_start_time/meeting_end_time 序列化成 "HH:MM"，
    這裡用 Optional[str] 直接接（不要用 time，否則 from_attributes 會
    對應到 ORM 的 Time 物件，產出再序列化變 ISO）。
    """

    name: str
    price: int
    sessions: Optional[int] = None
    frequency: str
    min_age_months: Optional[int] = None
    max_age_months: Optional[int] = None
    meeting_weekday: Optional[int] = None
    meeting_start_time: Optional[str] = None
    meeting_end_time: Optional[str] = None


class PublicSuppliesItemOut(IvyBaseModel):
    """GET /public/supplies 單筆。"""

    name: str
    price: int


class PublicRegistrationCourseOut(IvyBaseModel):
    """/public/query 與 /public/update 的 courses[] 單筆。"""

    name: str
    course_id: int
    price: int
    status: str
    waitlist_position: Optional[int] = None
    waitlist_total: Optional[int] = None
    confirm_deadline: Optional[str] = None


class PublicFieldStateOut(IvyBaseModel):
    """前端 UI hint：班級欄位可改/不可改的衍生狀態。"""

    class_source: str
    class_editable: bool
    review_state: str


class PublicRegistrationDetailOut(IvyBaseModel):
    """/public/query、/public/query-by-token、/public/update 共用 response。

    /public/update 多帶一個 message 欄位（成功提示），query 系列為 None。
    """

    id: int
    name: str  # pii-allow: 家長前台檢視自己學生報名資料
    birthday: Optional[str] = None  # pii-allow: 家長前台檢視自己學生報名資料
    class_name: Optional[str] = None
    is_paid: bool
    paid_amount: int
    total_amount: int
    payment_status: str
    remark: str
    courses: list[PublicRegistrationCourseOut]
    supplies: list[str]
    field_state: PublicFieldStateOut
    updated_at: Optional[str] = None
    message: Optional[str] = None


class PublicRegisterResultOut(IvyBaseModel):
    """POST /public/register response（含 honeypot silent / silent-success / 真實成功 三 path 同 shape）。

    query_token 為明文 token 只在這次回給家長一次，後續走 /public/query-by-token 用。
    """

    message: str
    id: int
    waitlisted: bool
    waitlist_courses: list[str]
    query_token: str  # pii-allow: 明文查詢碼，僅此 response 回給家長一次，DB 只存 hash
