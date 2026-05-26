"""請假規則純邏輯 helper。"""

from datetime import date, timedelta
from utils.taipei_time import today_taipei

SUPPORTING_DOCUMENT_THRESHOLD_DAYS = 2
PERSONAL_ADVANCE_NOTICE_DAYS = 2
SICK_LEAVE_INCREMENT_HOURS = 4.0


def get_requested_calendar_days(start_date: date, end_date: date) -> int:
    """回傳請假區間的曆日天數（含首尾）。"""
    return (end_date - start_date).days + 1


def requires_supporting_document(start_date: date, end_date: date) -> bool:
    """超過兩天的請假需要附證明。"""
    return (
        get_requested_calendar_days(start_date, end_date)
        > SUPPORTING_DOCUMENT_THRESHOLD_DAYS
    )


def validate_portal_leave_rules(
    leave_type: str,
    start_date: date,
    end_date: date,
    leave_hours: float,
    *,
    today: date | None = None,
) -> None:
    """驗證教師入口送出請假時的業務規則。"""
    today = today or today_taipei()

    # 防禦深度:Python `-4 % 4 == 0` 與 `0 % 4 == 0` 會讓 sick 規則 silently 通過
    # 負時數 / 零時數申請。API layer 已擋 < 0.5,helper 自身仍應檢查正向性。
    # eval framework IV6 揭露此 gap。
    if leave_hours <= 0:
        raise ValueError(f"請假時數必須大於 0:{leave_hours}")

    if leave_type == "personal":
        earliest_allowed = today + timedelta(days=PERSONAL_ADVANCE_NOTICE_DAYS)
        if start_date < earliest_allowed:
            raise ValueError("事假需至少提前 2 日提出申請")

    if leave_type == "sick":
        if leave_hours % SICK_LEAVE_INCREMENT_HOURS != 0:
            raise ValueError("病假必須以 4 小時為單位申請")
