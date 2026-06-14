"""P1-4 回歸：班別 late/early 計算與「兩筆打卡齊全」脫鉤。

Bug（2026-06-13 深度 QA 發現）：
  Excel 匯入時，班別（shift）late/early 重算只在 punch_in 與 punch_out 都在時
  才套用班別時間（api/attendance/upload.py:437 / :686 的 `and ... and ...` 守衛）。
  晚班教師（13:00-22:00）漏打一筆卡 → 落回用預設 08:00/17:00 算 late_minutes
  （13:00 對 08:00 = 300 分假遲到）→ 每筆最多被扣一整天日薪。

修法：抽純函式 compute_shift_aware_status——late 只需 punch_in、early 只需
punch_out，皆以班別時間為基準；缺的一側維持 missing 旗標。
"""

from datetime import datetime

from utils.attendance_calc import compute_shift_aware_status


def _dt(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi)


def test_both_punches_on_time_normal():
    is_late, late_m, is_early, early_m, status = compute_shift_aware_status(
        punch_in_dt=_dt(2026, 7, 14, 13, 0),
        punch_out_dt=_dt(2026, 7, 14, 22, 0),
        shift_start_dt=_dt(2026, 7, 14, 13, 0),
        shift_end_dt=_dt(2026, 7, 14, 22, 0),
    )
    assert (is_late, late_m, is_early, early_m, status) == (
        False,
        0,
        False,
        0,
        "normal",
    )


def test_late_shift_punch_in_only_uses_shift_not_default():
    """晚班 13:00-22:00，13:30 上班、漏打下班卡 → late=30（對 13:00），非 330（對 08:00）。"""
    is_late, late_m, is_early, early_m, status = compute_shift_aware_status(
        punch_in_dt=_dt(2026, 7, 14, 13, 30),
        punch_out_dt=None,
        shift_start_dt=_dt(2026, 7, 14, 13, 0),
        shift_end_dt=_dt(2026, 7, 14, 22, 0),
    )
    assert is_late is True
    assert late_m == 30  # 13:30 - 13:00，而非 13:30 - 08:00 = 330
    assert is_early is False
    assert early_m == 0
    assert "late" in status and "missing_out" in status


def test_punch_out_only_early_leave_uses_shift():
    """晚班 13:00-22:00，漏打上班卡、21:00 下班 → early=60（對 22:00）+ missing_in。"""
    is_late, late_m, is_early, early_m, status = compute_shift_aware_status(
        punch_in_dt=None,
        punch_out_dt=_dt(2026, 7, 14, 21, 0),
        shift_start_dt=_dt(2026, 7, 14, 13, 0),
        shift_end_dt=_dt(2026, 7, 14, 22, 0),
    )
    assert is_late is False
    assert late_m == 0
    assert is_early is True
    assert early_m == 60
    assert "early_leave" in status and "missing_in" in status


def test_both_punches_late_and_early():
    is_late, late_m, is_early, early_m, status = compute_shift_aware_status(
        punch_in_dt=_dt(2026, 7, 14, 13, 15),
        punch_out_dt=_dt(2026, 7, 14, 21, 45),
        shift_start_dt=_dt(2026, 7, 14, 13, 0),
        shift_end_dt=_dt(2026, 7, 14, 22, 0),
    )
    assert is_late is True and late_m == 15
    assert is_early is True and early_m == 15
    assert status == "late+early_leave"


def test_overnight_shift_punch_in_only():
    """跨夜班 18:00-02:00（shift_end 次日），18:30 上班漏打下班 → late=30、不誤判早退。"""
    is_late, late_m, is_early, early_m, status = compute_shift_aware_status(
        punch_in_dt=_dt(2026, 7, 14, 18, 30),
        punch_out_dt=None,
        shift_start_dt=_dt(2026, 7, 14, 18, 0),
        shift_end_dt=_dt(2026, 7, 15, 2, 0),  # 次日 02:00
    )
    assert is_late is True and late_m == 30
    assert is_early is False and early_m == 0
    assert "missing_out" in status
