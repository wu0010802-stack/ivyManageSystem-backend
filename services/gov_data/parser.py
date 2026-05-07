"""政府資料 parser：raw JSON → 內部 dataclass。

每個 source 一個 parse_* 函式。schema 隨政府公告會變，本檔是唯一需要因應變動修改之處。
fixture 凍結於 tests/fixtures/gov_data/，新版 schema 變動時先更新 fixture 重跑測試。

民國年/西元年混用：
- mol_pension.生效日 是「民國年」: "1150101" → 西元 2026-01-01
- mol_minimum_wage.實施日期（民國） 實測為西元年: "20240101" → 西元 2024-01-01
- _to_date 兩種都支援；年份 < 1911 或 7 位數字串視為民國年，加 1911 得西元
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from services.gov_data.schemas import (
    LaborBracketsResult,
    LaborPremiumResult,
    MinimumWageResult,
    NhiBracketsResult,
    NhiPremiumResult,
    PensionResult,
)


class ParserError(ValueError):
    """raw payload 與 parser 預期 schema 不符。"""


def parse_mol_labor_brackets(raw: Any) -> LaborBracketsResult:
    """勞工保險投保薪資分級表。fixture 欄位：月投保薪資 / 投保薪資等級 / 月薪資總額 / 適用起日。"""
    rows = _ensure_list(raw, label="mol_labor_brackets")
    amounts: list[int] = []
    try:
        for row in rows:
            amount = _to_int(row.get("月投保薪資"))
            amounts.append(amount)
    except (KeyError, TypeError, ValueError) as exc:
        raise ParserError(f"parse_mol_labor_brackets: {exc}") from exc
    amounts = sorted(set(amounts))
    if not amounts:
        raise ParserError("mol_labor_brackets: empty amount list")
    return LaborBracketsResult(
        effective_year=_year_from_rows(rows, key="適用起日") or date.today().year,
        amounts=amounts,
        max_insured=max(amounts),
    )


def parse_mol_labor_premium(raw: Any) -> LaborPremiumResult:
    """勞工保險費分擔表。fixture 欄位：投保薪資 / 勞工應負擔保費金額 / 單位應負擔保費金額。"""
    rows = _ensure_list(raw, label="mol_labor_premium")
    by_amount: dict[int, dict[str, int]] = {}
    try:
        for row in rows:
            amount = _to_int(row.get("投保薪資"))
            by_amount[amount] = {
                "labor_employee": _to_int(row.get("勞工應負擔保費金額")),
                "labor_employer": _to_int(row.get("單位應負擔保費金額")),
            }
    except (KeyError, TypeError, ValueError) as exc:
        raise ParserError(f"parse_mol_labor_premium: {exc}") from exc
    if not by_amount:
        raise ParserError("mol_labor_premium: empty")
    return LaborPremiumResult(effective_year=date.today().year, by_amount=by_amount)


def parse_mol_pension(raw: Any) -> PensionResult:
    """勞退月提繳工資分級表。fixture 欄位：月提繳工資金額/月提繳執行業務所得金額 / 等級 / 生效日。"""
    rows = _ensure_list(raw, label="mol_pension")
    amounts: list[int] = []
    eff_year: int | None = None
    try:
        for row in rows:
            # 注意 key 包含斜線
            v = row.get("月提繳工資金額/月提繳執行業務所得金額")
            amounts.append(_to_int(v))
            if eff_year is None:
                eff_str = row.get("生效日")
                if eff_str:
                    eff_year = _to_date(eff_str).year
    except (KeyError, TypeError, ValueError) as exc:
        raise ParserError(f"parse_mol_pension: {exc}") from exc
    amounts = sorted(set(amounts))
    if not amounts:
        raise ParserError("mol_pension: empty")
    return PensionResult(
        effective_year=eff_year or date.today().year,
        amounts=amounts,
        max_insured=max(amounts),
    )


def parse_nhi_brackets(raw: Any) -> NhiBracketsResult:
    """健保投保金額分級表。fixture 欄位：月投保金額（元） / 投保等級 / 組別級距。"""
    rows = _ensure_list(raw, label="nhi_brackets")
    amounts: list[int] = []
    try:
        for row in rows:
            v = row.get("月投保金額（元）")
            amounts.append(_to_int(v))
    except (KeyError, TypeError, ValueError) as exc:
        raise ParserError(f"parse_nhi_brackets: {exc}") from exc
    amounts = sorted(set(amounts))
    if not amounts:
        raise ParserError("nhi_brackets: empty")
    return NhiBracketsResult(
        effective_year=date.today().year,
        amounts=amounts,
        max_insured=max(amounts),
    )


def parse_nhi_premium(raw: Any) -> NhiPremiumResult:
    """健保保險費負擔金額表。

    fixture 欄位：
    - 月投保金額
    - 本人負擔金額（負擔比率30%）  ← single.employee
    - 投保單位負擔金額（負擔比率60%）  ← single.employer
    - 本人+N眷口負擔金額  ← deps[N]（僅供員工本人含眷屬保費參考）
    """
    rows = _ensure_list(raw, label="nhi_premium")
    by_amount: dict[int, dict] = {}
    try:
        for row in rows:
            amount = _to_int(row.get("月投保金額"))
            single = {
                "employee": _to_int(row.get("本人負擔金額（負擔比率30%）")),
                "employer": _to_int(row.get("投保單位負擔金額（負擔比率60%）")),
            }
            deps: dict[int, int] = {}
            for n in (1, 2, 3):
                v = row.get(f"本人+{n}眷口負擔金額")
                if v is not None and v != "":
                    deps[n] = _to_int(v)
            by_amount[amount] = {"single": single, "deps": deps}
    except (KeyError, TypeError, ValueError) as exc:
        raise ParserError(f"parse_nhi_premium: {exc}") from exc
    if not by_amount:
        raise ParserError("nhi_premium: empty")
    return NhiPremiumResult(effective_year=date.today().year, by_amount=by_amount)


_MW_MONTHLY_RE = re.compile(r"月薪\s*([0-9,]+)")
_MW_HOURLY_RE = re.compile(r"時薪\s*([0-9,]+)")


def parse_mol_minimum_wage(raw: Any) -> MinimumWageResult:
    """基本工資調整經過。

    fixture 欄位：
    - 實施日期（民國）  例 "20240101"（實測為西元年）
    - 內容/調整金額（新台幣）  例 "月薪25,250、時薪168"

    用 regex 從合併文字抽出 monthly / hourly。
    """
    rows = _ensure_list(raw, label="mol_minimum_wage")
    history: list[tuple[date, int, int]] = []
    try:
        for row in rows:
            eff_str = row.get("實施日期（民國）")
            text = row.get("內容/調整金額（新台幣）") or ""
            if not eff_str or not text:
                continue
            eff = _to_date(eff_str)
            mm = _MW_MONTHLY_RE.search(text)
            hh = _MW_HOURLY_RE.search(text)
            if not mm or not hh:
                # 部分舊年度可能只有月薪沒時薪；跳過不完整項
                continue
            monthly = _to_int(mm.group(1))
            hourly = _to_int(hh.group(1))
            history.append((eff, monthly, hourly))
    except (KeyError, TypeError, ValueError) as exc:
        raise ParserError(f"parse_mol_minimum_wage: {exc}") from exc
    history.sort(key=lambda t: t[0])
    if not history:
        raise ParserError("mol_minimum_wage: empty parsed history")
    return MinimumWageResult(history=history)


# ----- helpers -----


def _ensure_list(raw: Any, label: str = "") -> list[dict]:
    """政府 API 有時是 list、有時是 {"result": {"records": [...]}}。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("result", "data", "records"):
            inner = raw.get(key)
            if isinstance(inner, list):
                return inner
            if isinstance(inner, dict) and isinstance(inner.get("records"), list):
                return inner["records"]
    raise ParserError(
        f"{label}: cannot extract list from payload top-level type {type(raw).__name__}"
    )


def _to_int(value: Any) -> int:
    if value is None:
        raise ParserError("missing int field")
    if isinstance(value, bool):
        raise ParserError("bool not int")
    if isinstance(value, int):
        return value
    s = str(value).replace(",", "").strip()
    if not s:
        raise ParserError("empty int string")
    return int(float(s))


def _to_date(value: Any) -> date:
    """支援多種格式：
    - YYYY-MM-DD / YYYY/MM/DD / YYYYMMDD（西元）
    - 民國年純數字 7 位：YYYMMDD (e.g. "1150101" = 民國 115 = 西元 2026)
    - 民國年文字：「115年1月1日」
    """
    if isinstance(value, date):
        return value
    s = str(value).strip()
    # 西元標準格式
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # 民國年純數字 (7 位數 YYYMMDD)
    if s.isdigit() and len(s) == 7:
        yy = int(s[:3]) + 1911
        return date(yy, int(s[3:5]), int(s[5:7]))
    # 民國年文字格式
    if "年" in s and "月" in s:
        normalized = (
            s.replace("年", "/").replace("月", "/").replace("日", "").replace("-", "/")
        )
        parts = [p.strip() for p in normalized.split("/") if p.strip()]
        if len(parts) == 3:
            y, m, d = parts
            yy = int(y)
            if yy < 1911:
                yy += 1911
            return date(yy, int(m), int(d))
    raise ParserError(f"cannot parse date: {value}")


def _year_from_rows(rows: list[dict], key: str) -> int | None:
    """抽 row 中 key 的日期年份，作為 effective_year 推斷。"""
    for row in rows:
        v = row.get(key)
        if v:
            try:
                return _to_date(v).year
            except ParserError:
                continue
    return None
