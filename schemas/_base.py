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

序列化機制（系統設計審查 2026-06-14, top#4）：
    改用 **type-based json_encoders** 而非 wildcard `field_serializer("*")`。
    原 `field_serializer("*", ...) -> Any` 會讓 Pydantic 對【每一個】欄位（含純
    str/int/float/bool）的 serialization JSON schema 退化為「無 type」，導致
    OpenAPI → TS codegen 產出的 `schema.d.ts` 欄位型別全部變 unknown（即使端點
    已正確帶 response_model），整套型別防漂移管線被自身 base class 架空。
    改 json_encoders 後 **runtime JSON 輸出完全不變**（已實測逐欄位相同：
    Decimal→2 位小數 float、datetime→Asia/Taipei ISO、date→ISO），但純型別欄位的
    schema type 得以保留，codegen 重新產出正確型別。
    ⚠ json_encoders 在 Pydantic v2 已 deprecated（規劃於 v3 移除）；屆時升級
    Pydantic v3 時須改以 `Annotated[T, PlainSerializer(..., return_type=...)]`
    逐型別替代。專案 pytest filterwarnings 已 `ignore::DeprecationWarning`，CI 不受
    影響。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

_TAIPEI = ZoneInfo("Asia/Taipei")


def _serialize_datetime(value: datetime) -> str:
    """datetime → Asia/Taipei ISO 字串（無 tz 時當作 Taipei naive）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=_TAIPEI)
    return value.astimezone(_TAIPEI).isoformat()


def _serialize_date(value: date) -> str:
    """date → ISO 字串。"""
    return value.isoformat()


def _serialize_decimal(value: Decimal) -> float:
    """Decimal → 2 位小數 float（與薪資 round_half_up rollout 對齊）。"""
    return float(value.quantize(Decimal("0.01")))


class IvyBaseModel(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        # type-based 序列化：datetime 為 date 的子類，json_encoders 以 MRO 最具體
        # 型別優先匹配（datetime 值走 datetime encoder、純 date 走 date encoder）。
        json_encoders={
            datetime: _serialize_datetime,
            date: _serialize_date,
            Decimal: _serialize_decimal,
        },
    )
