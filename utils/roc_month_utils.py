"""民國年月工具模組。

本檔集中所有 `YYY.MM` 格式的解析、正規化、排序與偏移邏輯，
供 api/recruitment、api/config 等模組共用，避免重複實作。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

# "114.09.16" 的視訪日期擷取月份 regex
VISIT_DATE_MONTH_RE = re.compile(r"(?<!\d)(\d{3})[./-](\d{1,2})[./-]\d{1,2}")

# "114.09.16~115.03.15" 或 "114.09.16-115.03.15" 的期間 regex
PERIOD_RANGE_RE = re.compile(r"(\d{3}\.\d{2})\.\d{2}[~\-](\d{3}\.\d{2})\.\d{2}")


def normalize_roc_month(value: Optional[str]) -> Optional[str]:
    """正規化民國月份字串為 YYY.MM（例：115.3 → 115.03）。

    Raises:
        ValueError: 格式不正確時拋出，caller 應準備處理。
    """
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    parts = text.split(".")
    if len(parts) != 2:
        raise ValueError("月份格式應為 民國年.月，如 115.03")

    try:
        year_num = int(parts[0])
        month_num = int(parts[1])
    except ValueError as exc:
        raise ValueError("月份格式錯誤") from exc

    if year_num <= 0:
        raise ValueError(f"年份須為正整數，收到 {parts[0]}")
    if not (1 <= month_num <= 12):
        raise ValueError(f"月份須在 1-12 之間，收到 {month_num}")

    return f"{year_num}.{month_num:02d}"


def extract_roc_month_from_visit_date(value: Optional[str]) -> Optional[str]:
    """從視訪日期字串（如 114.09.16）擷取 YYY.MM 月份。"""
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    match = VISIT_DATE_MONTH_RE.search(text)
    if not match:
        return None

    year_num = int(match.group(1))
    month_num = int(match.group(2))
    if year_num <= 0 or not (1 <= month_num <= 12):
        return None
    return f"{year_num}.{month_num:02d}"


def safe_normalize_roc_month(value: Optional[str]) -> Optional[str]:
    """盡量正規化月份，若既有資料異常則保留原值而非拋例外。"""
    if value is None:
        return None
    try:
        return normalize_roc_month(value)
    except ValueError:
        stripped = value.strip()
        return stripped or None


def roc_month_sort_key(value: Optional[str]) -> tuple:
    """用於 `sorted(..., key=)` 的 key function。"""
    normalized = safe_normalize_roc_month(value)
    if normalized in (None, "", "未知"):
        return (999999, 99, normalized or "")

    parts = normalized.split(".")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return (999998, 99, normalized)

    return (int(parts[0]), int(parts[1]), normalized)


def parse_roc_month_parts(value: Optional[str]) -> Optional[tuple[int, int]]:
    """回傳 (year, month) tuple；格式錯誤則 raise ValueError。"""
    normalized = normalize_roc_month(value)
    year_text, month_text = normalized.split(".")
    return int(year_text), int(month_text)


def shift_roc_month(value: Optional[str], delta_months: int) -> Optional[str]:
    """對民國月份做月數偏移（正負皆可）。"""
    if value in (None, ""):
        return None

    year_num, month_num = parse_roc_month_parts(value)
    total_months = year_num * 12 + (month_num - 1) + delta_months
    if total_months < 0:
        return None

    shifted_year = total_months // 12
    shifted_month = total_months % 12 + 1
    return f"{shifted_year}.{shifted_month:02d}"


def roc_month_start(value: Optional[str]) -> Optional[datetime]:
    """將民國月份轉為該月第一天的西元 datetime（用於 DB 查詢比對）。"""
    if value in (None, ""):
        return None
    year_num, month_num = parse_roc_month_parts(value)
    return datetime(year_num + 1911, month_num, 1)


def expand_roc_month_range(start_ym: str, end_ym: str) -> set[str]:
    """回傳從 start_ym 到 end_ym（含）所有月份字串的集合。"""
    start_year, start_month = parse_roc_month_parts(start_ym)
    end_year, end_month = parse_roc_month_parts(end_ym)
    start_total = start_year * 12 + (start_month - 1)
    end_total = end_year * 12 + (end_month - 1)
    if start_total > end_total:
        start_total, end_total = end_total, start_total

    months: set[str] = set()
    for idx in range(start_total, end_total + 1):
        y = idx // 12
        m = idx % 12 + 1
        months.add(f"{y}.{m:02d}")
    return months
