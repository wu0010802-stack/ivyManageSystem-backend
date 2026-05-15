"""utils/leave_overtime_conflict.py — leaves ↔ overtimes 衝突檢查共用 helper。

第一波抽取：時間區間比對 + 型別正規化（leaves.py 與 overtimes.py 原本各自
維護一份相同邏輯）。完整衝突查詢（_check_overlap / _check_employee_has_
conflicting_*）仍在各 router 內，未來再分批搬。

公開：
- to_time(val) — str ('HH:MM') / datetime.time / datetime.datetime → time
- times_overlap(start1, end1, start2, end2) — 開放端點重疊判斷
"""

from datetime import datetime, time as dt_time


def to_time(val) -> dt_time:
    """str / datetime.time / datetime.datetime 統一正規化為 datetime.time。

    DB 欄位依設定不同可能回 time（Time 欄位）或 datetime（DateTime 欄位）；
    外部輸入則為 'HH:MM' 字串。混型比較會 TypeError，此 helper 確保任何
    輸入都能安全轉換為可比較的 datetime.time。
    """
    if isinstance(val, str):
        h, m = map(int, val.strip().split(":")[:2])
        return dt_time(h, m)
    if isinstance(val, datetime):
        # datetime 是 date 的子類別，必須在 date 之前檢查
        return val.time()
    if isinstance(val, dt_time):
        return val
    raise TypeError(f"無法將 {type(val).__name__!r} 轉為 datetime.time")


def times_overlap(start1, end1, start2, end2) -> bool:
    """判斷兩個時間區間是否重疊（開放端點：端點相接不視為重疊）。

    公式：start1 < end2 AND start2 < end1
    """
    return to_time(start1) < to_time(end2) and to_time(start2) < to_time(end1)
