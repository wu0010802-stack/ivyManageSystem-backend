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

# 與 api/activity/_shared.py 內既有常數對齊。schemas 層獨立 constant 避免雙向 import。
MAX_PAYMENT_AMOUNT = 99_999


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
