"""管理端行事曆 admin_feed Pydantic schemas。"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 與 utils.calendar_colors.ALL_LAYERS 保持同步；新增 layer 兩處都要改
# （runtime 同步檢查見 tests/test_calendar_admin_schemas.py::test_layer_literal_matches_constants）
Layer = Literal["event", "holiday", "leave", "activity", "appraisal", "meeting"]


class CalendarFeedItem(BaseModel):
    """單筆行事曆事件，統一 envelope。"""

    # union 順序：date 在前 — pydantic v2 smart mode 下 "YYYY-MM-DD" 解析為
    # date 物件 (all-day 維持 Phase A 行為)；"YYYY-MM-DDTHH:MM:SS" 因 date
    # strict format 不收，fallback 到 datetime。Phase B 新增 datetime 支援。
    layer: Layer
    id: int | str
    title: str
    start: date | datetime
    end: date | datetime
    all_day: bool = True
    color: str
    link: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class CalendarFeedResponse(BaseModel):
    """admin_feed 回應主體。"""

    model_config = ConfigDict(populate_by_name=True)

    from_: date = Field(alias="from")
    to: date
    items: list[CalendarFeedItem]
