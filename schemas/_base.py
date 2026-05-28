"""IvyBaseModel — 全 ivy-backend Out/In schema 共用 base。

提供：
- from_attributes=True：可從 SQLAlchemy ORM instance 直接 .model_validate()
- populate_by_name=True：允許 alias 與原名同時 populate（前端命名兼容）
- str_strip_whitespace=True：input 字串自動 trim
- datetime / date：序列化為 Asia/Taipei ISO 字串（無 tz 時當作 Taipei naive）
- Decimal：序列化為 2 位小數 float（與既有薪資 round_half_up rollout 對齊）

不在這層做：
- PII 遮罩（在 router 端決定，schema 用 Optional 接 None）
- enum → str（Pydantic v2 預設行為 OK）
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_serializer

_TAIPEI = ZoneInfo("Asia/Taipei")


class IvyBaseModel(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    @field_serializer("*", when_used="json", check_fields=False)
    def _serialize_special(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=_TAIPEI)
            return value.astimezone(_TAIPEI).isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value.quantize(Decimal("0.01")))
        return value
