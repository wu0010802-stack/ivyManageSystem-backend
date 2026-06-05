"""Rounding helpers — 統一金額進位行為。

Why this module exists:
    Python 內建 `round()` 使用 ROUND_HALF_EVEN（銀行家進位）：
        round(0.5) == 0, round(2.5) == 2, round(4.5) == 4
    而政府單據（勞健保局繳費單、退稅、薪資申報）、PostgreSQL NUMERIC 型別 cast
    都採 ROUND_HALF_UP（一般人直覺進位）：
        round_half_up(0.5) == 1, round_half_up(2.5) == 3

    在 0.5 邊界，兩者結果 50% 機率不同。salary engine 內部用 builtin round()
    寫進 SalaryRecord，但 DB 端 NUMERIC(12,2) 用 HALF_AWAY_FROM_ZERO（對正數
    等同 HALF_UP）重新 quantize，導致同筆計算在「engine 端 round → DB 端 quantize」
    會在 .5 邊界產生 1 元 diff（audit `.scratch/decimal_audit/audit_report.md`
    Layer 3 證實 ~1.25% 場景觸發、最大 1 元 diff/筆）。

    本 module 提供 `round_half_up()` 讓金額計算統一採政府標準，
    避免「engine 算 442 / DB 存 443 / 對帳差 1 元」這類非預期分歧。

API:
    round_half_up(x)            → int   # 等同 builtin round(x) 但採 HALF_UP
    round_half_up(x, ndigits=2) → float # 同 builtin round(x, n) 行為對比

Refs:
    - `services/year_end/engine.py` `_q2`：既有 Decimal+HALF_UP 模式（年終獎金）
    - `services/appraisal/rule_applier.py` `_TWO_PLACES`：同上
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Union

Number = Union[int, float, Decimal]


def round_half_up(value: Number, ndigits: int = 0) -> Union[int, float]:
    """以 ROUND_HALF_UP 進位（政府/勞健保/PG NUMERIC 標準）。

    >>> round_half_up(0.5)
    1
    >>> round_half_up(2.5)
    3
    >>> round_half_up(442.5)
    443
    >>> round_half_up(442.4)
    442
    >>> round_half_up(0.125, 2)
    0.13

    Args:
        value:   待進位的數值（int / float / Decimal 皆可，None 視為 0）
        ndigits: 保留小數位數，預設 0（回 int）

    Returns:
        ndigits == 0 → int
        ndigits >  0 → float（quantize 後 cast 回 float 維持與 builtin round() 簽章一致）
    """
    if value is None:
        return 0 if ndigits == 0 else 0.0
    # str(value) 避免 IEEE 殘渣進入 Decimal（例：Decimal(0.1) != Decimal("0.1"))
    d = Decimal(str(value))
    if ndigits == 0:
        return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    quant = Decimal("1").scaleb(-ndigits)
    return float(d.quantize(quant, rounding=ROUND_HALF_UP))


def round_down(value: Number, ndigits: int = 0) -> Union[int, float]:
    """無條件捨去（ROUND_DOWN，朝零方向截斷）。

    用於員工端扣款（請假、遲到、早退）以對齊園所實務：扣款金額一律捨去小數，
    對員工有利（不會因進位多扣 1 元）。與 round_half_up 並存——勞健保/政府單據仍用
    round_half_up。

    >>> round_down(491.67)
    491
    >>> round_down(245.83)
    245
    >>> round_down(696.75)
    696

    Args:
        value:   待捨去的數值（int / float / Decimal 皆可，None 視為 0）
        ndigits: 保留小數位數，預設 0（回 int）
    Returns:
        ndigits == 0 → int；ndigits > 0 → float
    """
    if value is None:
        return 0 if ndigits == 0 else 0.0
    d = Decimal(str(value))
    if ndigits == 0:
        return int(d.quantize(Decimal("1"), rounding=ROUND_DOWN))
    quant = Decimal("1").scaleb(-ndigits)
    return float(d.quantize(quant, rounding=ROUND_DOWN))
