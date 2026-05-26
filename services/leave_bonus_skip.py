"""育嬰假/產假/流產假期間自動跳過獎金。

業務規則（業主慣例，對齊《義華薪資》Excel）：
- 員工某月有「產假 / 育嬰留職停薪 / 流產假」任一天覆蓋 → 該月不發節慶+超額獎金
- 婚假短暫，預設不在此名單（業主可後續微調）
- 該員工該月 supervisor_dividend 不受此規則影響（由 skip_payroll_bonuses 旗標處理）

Excel 案例：
- 郭玟秀 114.11.03~114.12.28 產假 + 115.01.09~115.07.09 育嬰假 → 整段期間無節慶獎金
- 陳品棻 108.10.21~12.15 產假 + 109.01.01~110.12.31 育嬰假 → 同上
"""

from __future__ import annotations

import calendar
from datetime import date

from sqlalchemy.orm import Session

from models.approval import ApprovalStatus
from models.database import LeaveRecord

# 預設「跳過獎金」的請假類型（任一天覆蓋該月 → 該月不發節慶+超額）
SKIP_BONUS_LEAVE_TYPES = frozenset(
    [
        "maternity",  # 產假
        "parental_unpaid",  # 育嬰留職停薪
        "miscarriage",  # 流產假
    ]
)


def should_skip_bonuses_for_month(
    session: Session,
    employee_id: int,
    year: int,
    month: int,
    *,
    leave_types: frozenset | set | None = None,
) -> tuple[bool, list[LeaveRecord]]:
    """判斷某月是否有「不發獎金」的假覆蓋。

    Args:
        leave_types: 自訂跳過清單；None=用 SKIP_BONUS_LEAVE_TYPES 預設

    Returns:
        (should_skip, matched_leaves)
        - should_skip: True=該月任一天屬於 skip 假別
        - matched_leaves: 命中的核准假單（供 UI 顯示原因）
    """
    types = leave_types or SKIP_BONUS_LEAVE_TYPES
    if not types:
        return False, []

    _, last_day = calendar.monthrange(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)

    leaves = (
        session.query(LeaveRecord)
        .filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.status == ApprovalStatus.APPROVED.value,
            LeaveRecord.leave_type.in_(list(types)),
            LeaveRecord.start_date <= month_end,
            LeaveRecord.end_date >= month_start,
        )
        .all()
    )
    return bool(leaves), leaves


def format_skip_reason(leaves: list[LeaveRecord]) -> str:
    """生成可讀的跳過原因字串。"""
    if not leaves:
        return ""
    LABELS = {
        "maternity": "產假",
        "parental_unpaid": "育嬰留職停薪",
        "miscarriage": "流產假",
    }
    parts = []
    for lv in leaves:
        label = LABELS.get(lv.leave_type, lv.leave_type)
        parts.append(f"{label} {lv.start_date.isoformat()}~{lv.end_date.isoformat()}")
    return "；".join(parts)
