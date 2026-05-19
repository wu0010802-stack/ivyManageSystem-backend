"""管理端行事曆 admin_feed Pydantic schemas。"""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 與 utils.calendar_colors.ALL_LAYERS 保持同步；新增 layer 兩處都要改
# （runtime 同步檢查見 tests/test_calendar_admin_schemas.py::test_layer_literal_matches_constants）
Layer = Literal["event", "holiday", "leave", "activity", "appraisal", "meeting"]


class CalendarFeedItem(BaseModel):
    """單筆行事曆事件，統一 envelope。"""

    layer: Layer
    id: int | str
    title: str
    start: date
    end: date
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
