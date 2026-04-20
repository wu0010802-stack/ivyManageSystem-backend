"""
自訂 SQLAlchemy 欄位型別。

- Money：PG 底層為 Numeric(12, 2)（精度 12、小數 2，上限 9,999,999,999.99），
  Python 讀出時自動轉 float，寫入時接受 float / int / str / Decimal。
  用於所有薪資金額欄位，避免 Float（double precision）累積誤差導致對帳尾數失真，
  同時保持與現有 Python float 計算邏輯相容（engine 不需重寫為 Decimal）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy.types import Numeric, TypeDecorator


class Money(TypeDecorator):
    impl = Numeric(12, 2)
    cache_ok = True

    def process_bind_param(self, value, dialect) -> Optional[Decimal]:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    def process_result_value(self, value, dialect) -> Optional[float]:
        if value is None:
            return None
        return float(value)
