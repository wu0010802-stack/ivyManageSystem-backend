"""
時薪制工時與薪資計算
"""

from datetime import date, datetime, time, timedelta
from typing import Optional

from .constants import MAX_DAILY_WORK_HOURS, HOURLY_OT1_RATE, HOURLY_OT2_RATE, HOURLY_REGULAR_HOURS, HOURLY_OT1_CAP_HOURS


def _calc_lunch_overlap_hours(start: datetime, end: datetime, ref_date: date) -> float:
    """計算 [start, end) 時段與指定日期 12:00-13:00 午休窗口的重疊時數。

    Args:
        start:    打卡開始時間
        end:      打卡結束時間（必須 > start）
        ref_date: 需計算午休的日期（跨夜班需分別以兩個日期各呼叫一次）
    Returns:
        重疊時數（0.0 ~ 1.0 小時）
    """
    lunch_s = datetime.combine(ref_date, time(12, 0))
    lunch_e = datetime.combine(ref_date, time(13, 0))
    return max(0.0, (min(end, lunch_e) - max(start, lunch_s)).total_seconds() / 3600)


def _compute_hourly_daily_hours(
    punch_in: datetime,
    punch_out: Optional[datetime],
    work_end_t: time,
) -> float:
    """計算時薪制員工單日實際工時（含午休扣除與時空穿越防護）。

    時空穿越防護：
    - 無下班打卡時，以 work_end_t 補填；若補填後下班 ≤ 上班 → 回傳 0.0
    - 有下班打卡但早於上班（管理員誤植等資料異常）→ 同樣回傳 0.0

    Args:
        punch_in:    上班打卡時間
        punch_out:   下班打卡時間（None 表示缺打）
        work_end_t:  排班預設下班時間（用於補填缺打）

    Returns:
        當日有效工時（小時），已扣午休、已套用每日上限，最小值 0.0
    """
    if punch_out is not None:
        effective_out = punch_out
    else:
        # 缺下班打卡：以排班下班時間代入，避免員工工時歸零
        effective_out = datetime.combine(punch_in.date(), work_end_t)
        # 跨夜班：排班下班時間在上班時間之前（如 work_end=02:00 < punch_in=18:00）
        # 若補一天後工時仍合理（≤ 每日上限），視為隔日下班
        if effective_out <= punch_in:
            candidate = effective_out + timedelta(days=1)
            if (candidate - punch_in).total_seconds() / 3600 <= MAX_DAILY_WORK_HOURS:
                effective_out = candidate

    # 防止時空穿越：補填或明確設定的下班時間若早於或等於上班時間，略過該日
    if effective_out <= punch_in:
        return 0.0

    diff = (effective_out - punch_in).total_seconds() / 3600
    # 扣除午休（12:00–13:00）：逐日檢查，涵蓋跨日班次。
    # effective_out 若在次日，次日的午休窗口同樣需納入計算，
    # 以避免跨夜班跨越隔日 12:00–13:00 時漏扣或誤扣午休時數。
    overlap = sum(
        _calc_lunch_overlap_hours(punch_in, effective_out, _d)
        for _d in sorted({punch_in.date(), effective_out.date()})
    )
    diff -= overlap
    # 每日工時上限；max(0.0,...) 為雙重保護，確保不因浮點誤差產生負值
    return max(0.0, min(diff, MAX_DAILY_WORK_HOURS))


def _calc_daily_hourly_pay(hours: float, rate: float) -> float:
    """依勞基法第 24 條計算時薪制員工單日薪資。

    分段計費：
    - 0–8 小時：正常倍率（×1.0）
    - 第 9–10 小時：×HOURLY_OT1_RATE（1.34）
    - 第 11 小時起：×HOURLY_OT2_RATE（1.67）

    Args:
        hours: 當日實際工時（已扣午休、已套用上限）
        rate:  時薪
    Returns:
        當日應付薪資（未四捨五入）
    """
    regular = min(hours, HOURLY_REGULAR_HOURS)
    ot1 = max(0.0, min(hours - HOURLY_REGULAR_HOURS,
                       HOURLY_OT1_CAP_HOURS - HOURLY_REGULAR_HOURS))
    ot2 = max(0.0, hours - HOURLY_OT1_CAP_HOURS)
    return rate * (regular + ot1 * HOURLY_OT1_RATE + ot2 * HOURLY_OT2_RATE)
