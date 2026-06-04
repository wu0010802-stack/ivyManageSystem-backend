"""provenance 介面：DerivedValue / SourceRecord。

每個「自動推導值」統一表達成 DerivedValue，供前端深度3 下鑽。
正確性保證（provider 測試）：Σ source_records.amount（四捨五入至分）== value。
is_override / override_meta 為手動覆寫預留欄（P2 才計算），此處只定義。
"""

from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field


class SourceRecord(BaseModel):
    """一筆原始來源紀錄（下鑽明細的一列）。"""

    date: Date = Field(description="紀錄日期")
    label: str = Field(description="可讀標籤，如『遲到』『事假 8h』")
    amount: Decimal = Field(description="此筆對 value 的貢獻（罰則為負）")
    module: str = Field(description="來源模組 key，如 attendance/leave/meeting")
    source_id: Optional[int] = Field(default=None, description="來源資料列 PK")


class DerivedValue(BaseModel):
    """一個自動推導值 + 其完整 provenance。"""

    key: str = Field(description="推導項 key，如 attendance_late")
    value: Decimal = Field(description="權威值（與既有引擎一致，不可漂移）")
    formula_summary: str = Field(description="可讀算式摘要")
    breakdown: dict[str, Any] = Field(
        default_factory=dict, description="結構化組成（次數/單價/期間…）"
    )
    source_records: list[SourceRecord] = Field(
        default_factory=list, description="逐筆原始紀錄"
    )
    deep_link: Optional[str] = Field(
        default=None, description="跳轉來源模組的前端路由+filter"
    )
    is_override: bool = Field(default=False, description="是否被手動覆寫（P2）")
    override_meta: Optional[dict[str, Any]] = Field(
        default=None, description="{原自動值, 覆寫者, 時間, 原因}（P2）"
    )
