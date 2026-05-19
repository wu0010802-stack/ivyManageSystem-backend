"""管理端行事曆 admin_feed Pydantic schemas。"""

from datetime import date
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

Layer = Literal["event", "holiday", "leave", "activity", "appraisal", "meeting"]


class CalendarFeedItem(BaseModel):
    """單筆行事曆事件，統一 envelope。"""

    layer: Layer
    id: Union[int, str]
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
