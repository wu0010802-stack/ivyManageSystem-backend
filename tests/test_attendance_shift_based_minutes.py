"""排班教師 legacy 匯入：旗標與扣款分鐘須同走排班基準（C15，bug hunt 2026-06-14）。

原 handler 對排班教師以 shift_start/shift_end 重算 is_late/is_early_leave/status，
但寫入的 late_minutes/early_leave_minutes 仍取 parser 預設 08:00/17:00 基準的 detail 值
→ 旗標與分鐘脫鉤，造成少扣（早班遲到被預設基準遮蔽）或多扣（晚班準時被預設基準誤判）。

修法：抽出純函式 compute_shift_based_attendance，旗標與分鐘同源以排班基準計算，
handler 將分鐘回填 detail 供寫入。
"""

from datetime import datetime

from api.attendance._shared import compute_shift_based_attendance


def _dt(h, m):
    return datetime(2026, 6, 1, h, m)


def test_late_minutes_use_shift_basis():
    # 排班 09:00-18:00，09:30 進 → 排班基準遲到 30 分
    is_late, is_early, late_min, early_min, status = compute_shift_based_attendance(
        _dt(9, 30), _dt(18, 0), _dt(9, 0), _dt(18, 0)
    )
    assert is_late is True
    assert late_min == 30
    assert is_early is False
    assert early_min == 0
    assert status == "late"


def test_early_shift_late_not_masked():
    # 排班 07:00-16:00，07:30 進 → 排班基準遲到 30 分。
    # 預設 08:00 基準會誤判「未遲到」→ 少扣（旗標 True 但分鐘 0）。
    is_late, is_early, late_min, early_min, status = compute_shift_based_attendance(
        _dt(7, 30), _dt(16, 0), _dt(7, 0), _dt(16, 0)
    )
    assert is_late is True
    assert late_min == 30
    assert status == "late"


def test_late_shift_ontime_not_overcounted():
    # 排班 09:00-18:00，08:30 進 → 排班基準未遲到。
    # 預設 08:00 基準會誤判「遲到 30 分」→ 多扣。
    is_late, is_early, late_min, early_min, status = compute_shift_based_attendance(
        _dt(8, 30), _dt(18, 0), _dt(9, 0), _dt(18, 0)
    )
    assert is_late is False
    assert late_min == 0
    assert status == "normal"


def test_early_leave_minutes_shift_basis():
    # 排班 09:00-18:00，17:30 出 → 早退 30 分
    is_late, is_early, late_min, early_min, status = compute_shift_based_attendance(
        _dt(9, 0), _dt(17, 30), _dt(9, 0), _dt(18, 0)
    )
    assert is_early is True
    assert early_min == 30
    assert is_late is False
    assert status == "early_leave"


def test_both_late_and_early_leave():
    is_late, is_early, late_min, early_min, status = compute_shift_based_attendance(
        _dt(9, 15), _dt(17, 45), _dt(9, 0), _dt(18, 0)
    )
    assert is_late is True and is_early is True
    assert late_min == 15 and early_min == 15
    assert status == "late+early_leave"


def test_on_time_full_shift_normal():
    is_late, is_early, late_min, early_min, status = compute_shift_based_attendance(
        _dt(9, 0), _dt(18, 0), _dt(9, 0), _dt(18, 0)
    )
    assert (is_late, is_early, late_min, early_min, status) == (
        False,
        False,
        0,
        0,
        "normal",
    )
