"""經營分析閾值、靜態對照與小型 helper。"""

from __future__ import annotations

from datetime import date
from typing import Optional, Tuple

# === 流失預警閾值（保守套餐）=========================================
CHURN_CONSECUTIVE_ABSENCE_DAYS = 3  # A: 連續缺勤工作日
CHURN_ON_LEAVE_DAYS = 30  # C: on_leave 未復學天數
CHURN_FEE_OVERDUE_DAYS = 14  # D: 學費逾期天數（自學期始日起算）

# === 漏斗階段定義 =====================================================
FUNNEL_STAGES = [
    "lead",  # 線索（visit + ParentInquiry）
    "deposit",  # 預繳意向
    "enrolled",  # 報名
    "active",  # 實際入學
    "retained_1m",  # 1 個月留存
    "retained_6m",  # 6 個月留存
]

FUNNEL_STAGE_LABELS = {
    "lead": "線索",
    "deposit": "預繳意向",
    "enrolled": "報名",
    "active": "入學",
    "retained_1m": "1 個月留存",
    "retained_6m": "6 個月留存",
}

RETENTION_WINDOWS_DAYS = {"1m": 30, "6m": 180}

# === 學期起始日（FeeItem.due_date proxy） =============================
# (term_no, (month, day))
_TERM_START_MD = {
    1: (9, 1),  # 上學期 9/1
    2: (2, 1),  # 下學期 2/1（隔年）
}


def parse_roc_month(raw: Optional[str]) -> Optional[Tuple[int, int]]:
    """解析民國月份字串 e.g. '115.03' → (2026, 3)。

    無效格式回 None，呼叫端決定是否略過或 log。
    """
    if not raw or not isinstance(raw, str):
        return None
    parts = raw.strip().split(".")
    if len(parts) != 2:
        return None
    try:
        roc_year = int(parts[0])
        month = int(parts[1])
    except ValueError:
        return None
    if not (1 <= month <= 12):
        return None
    return (roc_year + 1911, month)


def term_start_date(period: Optional[str]) -> Optional[date]:
    """解析 FeeItem.period e.g. '2025-1' → date(2025, 9, 1)；'2025-2' → date(2026, 2, 1)。

    無效格式回 None。
    """
    if not period or not isinstance(period, str):
        return None
    parts = period.strip().split("-")
    if len(parts) != 2:
        return None
    try:
        year = int(parts[0])
        term = int(parts[1])
    except ValueError:
        return None
    md = _TERM_START_MD.get(term)
    if md is None:
        return None
    month, day = md
    if term == 2:
        year += 1  # 下學期落在年度+1 的 2/1
    return date(year, month, day)
